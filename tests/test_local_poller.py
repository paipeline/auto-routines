"""
Tests for scripts/local_poller.py (PRD #10 OQ4).

Architecture decision baked into these tests:

    `repository_dispatch` events are NOT queryable — they're a write-only
    trigger surface. Our local-fire dispatches in auto-routines.yml were
    therefore being emitted into the void; nothing local could consume
    them. Instead, the workflow will append to an event-log file
    `.iteration/local_dispatches.jsonl` (one JSON object per line) which
    the poller reads with a watermark. This file is committed back to
    main alongside state.json on every tick, so local pollers just
    `git fetch` to see new fires.

    `last_dispatch` in state.json keeps its existing meaning: a "what
    happened last for each routine" map for dashboard/sanity purposes.
    The append-only log is the queue. Different responsibilities, no
    double duty.

This first test slice covers the deep module — the pure parsing
function. CLI shim, file I/O, and subprocess fan-out are tested at the
edges (CLI argparse + subprocess smoke). Workflow wiring + Stop-hook
install land in follow-up PRs.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
POLLER_PATH = ROOT / "scripts" / "local_poller.py"


@pytest.fixture(scope="module")
def poller():
    """Load scripts/local_poller.py as a module without polluting sys.path
    or relying on a package layout (scripts/ has no __init__.py)."""
    assert POLLER_PATH.exists(), f"PRD #10 OQ4 requires {POLLER_PATH}"
    spec = importlib.util.spec_from_file_location("local_poller", POLLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure: parse_log_lines — JSONL → list[FireRequest]
# ---------------------------------------------------------------------------

class TestParseLogLines:
    def test_empty_input_returns_empty(self, poller):
        assert poller.parse_log_lines([]) == []

    def test_single_well_formed_entry(self, poller):
        line = json.dumps({
            "event_id": 1,
            "routine_id": "session-doc-drift",
            "ts": "2026-05-10T17:03:00-0700",
            "sha": "abc123",
        })
        out = poller.parse_log_lines([line])
        assert len(out) == 1
        assert out[0]["event_id"] == 1
        assert out[0]["routine_id"] == "session-doc-drift"

    def test_multiple_entries_preserved_in_order(self, poller):
        lines = [
            json.dumps({"event_id": 1, "routine_id": "a", "ts": "2026-05-10T17:03:00-0700", "sha": "x"}),
            json.dumps({"event_id": 2, "routine_id": "b", "ts": "2026-05-10T17:04:00-0700", "sha": "y"}),
            json.dumps({"event_id": 3, "routine_id": "c", "ts": "2026-05-10T17:05:00-0700", "sha": "z"}),
        ]
        out = poller.parse_log_lines(lines)
        assert [r["routine_id"] for r in out] == ["a", "b", "c"]

    def test_blank_lines_ignored(self, poller):
        """A trailing newline on the file produces an empty final line.
        Don't crash on it."""
        lines = [
            "",
            json.dumps({"event_id": 1, "routine_id": "a", "ts": "2026-05-10T17:03:00-0700", "sha": "x"}),
            "   ",
            "",
        ]
        out = poller.parse_log_lines(lines)
        assert len(out) == 1
        assert out[0]["routine_id"] == "a"

    def test_malformed_json_raises_with_line_number(self, poller):
        """A corrupt line should fail loud, not silently drop. The error
        must include the offending line number so the user can fix it.
        Loose-mode skip would mask data loss."""
        lines = [
            json.dumps({"event_id": 1, "routine_id": "a", "ts": "2026-05-10T17:03:00-0700", "sha": "x"}),
            "this is not json",
        ]
        with pytest.raises(ValueError, match=r"line 2"):
            poller.parse_log_lines(lines)

    def test_missing_required_field_raises(self, poller):
        """Each entry must carry event_id + routine_id + ts + sha. Missing
        any of those breaks the watermark contract."""
        lines = [json.dumps({"routine_id": "a", "ts": "2026-05-10T17:03:00-0700", "sha": "x"})]
        with pytest.raises(ValueError, match="event_id"):
            poller.parse_log_lines(lines)

    def test_event_id_must_be_strict_int(self, poller):
        """isinstance(True, int) is True in Python. Reject bools so the
        watermark comparison can never silently misorder entries."""
        lines = [json.dumps({
            "event_id": True, "routine_id": "a",
            "ts": "2026-05-10T17:03:00-0700", "sha": "x",
        })]
        with pytest.raises(ValueError, match="event_id"):
            poller.parse_log_lines(lines)

    def test_routine_id_must_be_kebab_case(self, poller):
        """Same kebab-case discipline as state.py / sanity-check.py.
        A bad id can't possibly map to a real routine, so fail at parse."""
        lines = [json.dumps({
            "event_id": 1, "routine_id": "Bad_ID",
            "ts": "2026-05-10T17:03:00-0700", "sha": "x",
        })]
        with pytest.raises(ValueError, match="routine_id"):
            poller.parse_log_lines(lines)

    def test_ts_must_have_explicit_offset_no_z(self, poller):
        """UTC `Z` is banned per state.py / SKILL.md — local-time logs
        with explicit offset only. Catch it at parse, not later."""
        lines = [json.dumps({
            "event_id": 1, "routine_id": "a",
            "ts": "2026-05-10T17:03:00Z", "sha": "x",
        })]
        with pytest.raises(ValueError, match=r"(?i)offset|ts"):
            poller.parse_log_lines(lines)


# ---------------------------------------------------------------------------
# Pure: filter_new — given parsed entries + watermark, what fires?
# ---------------------------------------------------------------------------

class TestFilterNew:
    def _entry(self, event_id, rid="a"):
        return {
            "event_id": event_id, "routine_id": rid,
            "ts": "2026-05-10T17:03:00-0700", "sha": "x",
        }

    def test_watermark_zero_returns_everything(self, poller):
        entries = [self._entry(1), self._entry(2), self._entry(3)]
        assert poller.filter_new(entries, watermark=0) == entries

    def test_watermark_excludes_seen(self, poller):
        entries = [self._entry(1), self._entry(2), self._entry(3)]
        out = poller.filter_new(entries, watermark=2)
        assert [e["event_id"] for e in out] == [3]

    def test_watermark_at_or_above_max_returns_empty(self, poller):
        entries = [self._entry(1), self._entry(2), self._entry(3)]
        assert poller.filter_new(entries, watermark=3) == []
        assert poller.filter_new(entries, watermark=99) == []

    def test_out_of_order_event_ids_handled(self, poller):
        """Don't assume sorted input — the workflow appends in order, but
        a hand-edit, merge, or replay could reorder. Filter on the
        comparison, not on position."""
        entries = [self._entry(3), self._entry(1), self._entry(2)]
        out = poller.filter_new(entries, watermark=1)
        # All entries with event_id > 1
        assert sorted(e["event_id"] for e in out) == [2, 3]

    def test_duplicate_event_ids_all_returned(self, poller):
        """Duplicates would indicate a workflow bug, but the poller
        shouldn't dedupe silently — surfacing a duplicate downstream
        is the right move (subprocess fan-out is idempotent enough)."""
        entries = [self._entry(2), self._entry(2)]
        out = poller.filter_new(entries, watermark=1)
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Pure: max_event_id — watermark advancement
# ---------------------------------------------------------------------------

class TestMaxEventId:
    def test_empty_returns_input_watermark(self, poller):
        """No new entries → don't move the watermark backward, just keep
        the current value."""
        assert poller.max_event_id([], current=5) == 5

    def test_uses_max_across_entries(self, poller):
        entries = [
            {"event_id": 3, "routine_id": "a", "ts": "x", "sha": "y"},
            {"event_id": 7, "routine_id": "a", "ts": "x", "sha": "y"},
            {"event_id": 5, "routine_id": "a", "ts": "x", "sha": "y"},
        ]
        assert poller.max_event_id(entries, current=0) == 7

    def test_never_regresses_below_current(self, poller):
        """If the log somehow has lower event_ids than the current
        watermark (clock skew, partial fetch), the watermark stays put."""
        entries = [{"event_id": 1, "routine_id": "a", "ts": "x", "sha": "y"}]
        assert poller.max_event_id(entries, current=10) == 10


# ---------------------------------------------------------------------------
# CLI shim — argparse + stdout/stderr injection (no subprocess yet)
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_main_returns_int(self, poller):
        """cli_main must return an int exit code so __main__ can sys.exit
        on it. Crashing through bare exceptions is a worse caller
        contract."""
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(["--help"], stdout=out, stderr=err)
        assert isinstance(rc, int)

    def test_cli_unknown_subcommand_errors(self, poller):
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(["nonsense"], stdout=out, stderr=err)
        assert rc != 0

    def test_cli_scan_dry_run(self, poller, tmp_path):
        """`scan --log <file> --watermark N --dry-run` should parse the
        log + emit a JSON list of pending fires to stdout, without doing
        any side-effects (no subprocess, no watermark write)."""
        log = tmp_path / "local_dispatches.jsonl"
        log.write_text(
            json.dumps({
                "event_id": 1, "routine_id": "session-doc-drift",
                "ts": "2026-05-10T17:03:00-0700", "sha": "abc",
            }) + "\n"
        )
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["scan", "--log", str(log), "--watermark", "0", "--dry-run"],
            stdout=out, stderr=err,
        )
        assert rc == 0, err.getvalue()
        payload = json.loads(out.getvalue())
        assert isinstance(payload, dict)
        assert "pending" in payload
        assert len(payload["pending"]) == 1
        assert payload["pending"][0]["routine_id"] == "session-doc-drift"
        assert payload["next_watermark"] == 1

    def test_cli_scan_missing_log_is_not_an_error(self, poller, tmp_path):
        """A missing log file just means 'no fires yet' — fresh install
        case. Don't fail, just emit empty pending."""
        log = tmp_path / "does_not_exist.jsonl"
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["scan", "--log", str(log), "--watermark", "0", "--dry-run"],
            stdout=out, stderr=err,
        )
        assert rc == 0, err.getvalue()
        payload = json.loads(out.getvalue())
        assert payload["pending"] == []
        assert payload["next_watermark"] == 0


# ---------------------------------------------------------------------------
# Subprocess smoke — actually executable as a script
# ---------------------------------------------------------------------------

class TestSubprocessSmoke:
    def test_runs_as_script(self, tmp_path):
        """The Stop hook will invoke this as a plain `python …` call.
        Catch any import-time crash before the user does."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(POLLER_PATH), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "scan" in result.stdout
