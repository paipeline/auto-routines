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


# ---------------------------------------------------------------------------
# Pure: build_fire_command — what subprocess do we exec for a fire?
# ---------------------------------------------------------------------------

class TestBuildFireCommand:
    def test_basic_shape(self, poller):
        """The command must invoke `claude` with a --skill flag pointing
        at the routine id. Exact other flags can change; pin the contract
        bits (binary + skill targeting)."""
        cmd = poller.build_fire_command("session-doc-drift")
        assert isinstance(cmd, list)
        assert cmd[0] == "claude"
        # --skill comes from Module 4's per-routine spawn
        joined = " ".join(cmd)
        assert "session-doc-drift" in joined
        assert "--skill" in cmd

    def test_uses_dangerously_skip_permissions(self, poller):
        """Local fan-out is non-interactive; hook context can't answer
        permission prompts. Skip-perms is the right tradeoff (the user
        already trusted the routine when they enabled it in config)."""
        cmd = poller.build_fire_command("session-doc-drift")
        assert "--dangerously-skip-permissions" in cmd

    def test_routine_id_passed_unchanged(self, poller):
        """No quoting / mangling — kebab-case ids must round-trip
        verbatim or the routine fails to load."""
        cmd = poller.build_fire_command("daily-digest")
        # Last positional should be the id (or whatever follows --skill)
        i = cmd.index("--skill")
        assert cmd[i + 1] == "daily-digest"


# ---------------------------------------------------------------------------
# fire subcommand — subprocess fan-out with injectable runner
# ---------------------------------------------------------------------------

class FakeRunner:
    """Captures fire calls so tests can assert without spawning real
    subprocesses. Returns whatever exit_codes were queued."""

    def __init__(self, exit_codes=None):
        self.calls = []
        self._exit_codes = list(exit_codes or [])

    def __call__(self, cmd, *, timeout=None):
        self.calls.append({"cmd": cmd, "timeout": timeout})
        return self._exit_codes.pop(0) if self._exit_codes else 0


class TestFireSubcommand:
    def test_fire_dispatches_each_pending_entry(self, poller, tmp_path):
        """One pending entry → one subprocess. Two → two. The runner
        callable is injected so we don't actually spawn `claude`."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "session-doc-drift",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
            + json.dumps({"event_id": 2, "routine_id": "daily-digest",
                          "ts": "2026-05-10T17:04:00-0700", "sha": "y"}) + "\n"
        )
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["fire", "--log", str(log), "--watermark", "0"],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert len(runner.calls) == 2
        ids = [c["cmd"][c["cmd"].index("--skill") + 1] for c in runner.calls]
        assert ids == ["session-doc-drift", "daily-digest"]

    def test_fire_skips_already_seen(self, poller, tmp_path):
        """Watermark filters before fan-out — entries at or below it
        don't fire."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
            + json.dumps({"event_id": 2, "routine_id": "b",
                          "ts": "2026-05-10T17:04:00-0700", "sha": "y"}) + "\n"
        )
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["fire", "--log", str(log), "--watermark", "1"],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert len(runner.calls) == 1
        assert runner.calls[0]["cmd"][runner.calls[0]["cmd"].index("--skill") + 1] == "b"

    def test_fire_reports_outcomes_in_json(self, poller, tmp_path):
        """Stdout payload must include each fire's outcome (rc) so the
        Stop hook / operator can see what happened. Otherwise a silent
        failure looks like success."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
            + json.dumps({"event_id": 2, "routine_id": "b",
                          "ts": "2026-05-10T17:04:00-0700", "sha": "y"}) + "\n"
        )
        # First exits 0, second exits 7
        runner = FakeRunner(exit_codes=[0, 7])
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["fire", "--log", str(log), "--watermark", "0"],
            stdout=out, stderr=err, runner=runner,
        )
        # Top-level rc is non-zero because at least one fire failed
        assert rc != 0, "rc must surface partial failure to the caller"
        payload = json.loads(out.getvalue())
        assert "fires" in payload
        assert len(payload["fires"]) == 2
        outcomes = {f["routine_id"]: f["exit_code"] for f in payload["fires"]}
        assert outcomes == {"a": 0, "b": 7}

    def test_fire_advances_next_watermark(self, poller, tmp_path):
        """Even on partial failure, the watermark advances — otherwise we
        infinitely retry a broken routine. Workflow will refire the
        condition next tick if it still holds."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 5, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
        )
        runner = FakeRunner(exit_codes=[1])  # failure
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["fire", "--log", str(log), "--watermark", "0"],
            stdout=out, stderr=err, runner=runner,
        )
        payload = json.loads(out.getvalue())
        assert payload["next_watermark"] == 5
        assert rc != 0  # still surfaces the routine failure

    def test_fire_no_pending_is_clean_exit(self, poller, tmp_path):
        """No pending entries → exit 0, no subprocess, payload reports
        zero fires. Hook firing on every Claude session must not noisy
        up the transcript."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
        )
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["fire", "--log", str(log), "--watermark", "1"],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0
        assert runner.calls == []
        payload = json.loads(out.getvalue())
        assert payload["fires"] == []
        assert payload["next_watermark"] == 1

    def test_fire_missing_log_is_clean_exit(self, poller, tmp_path):
        """Same fresh-install case as scan — missing log means no fires
        yet, not a hard error."""
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["fire", "--log", str(tmp_path / "nope.jsonl"), "--watermark", "0"],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert runner.calls == []

    def test_fire_default_runner_is_real_subprocess(self, poller):
        """Sanity check: production callers don't pass a runner. The
        default must be the real subprocess wrapper (otherwise nothing
        would actually fire)."""
        # We don't actually fire — just check the default exists and
        # is callable. The wrapper module name should mention subprocess
        # somewhere visible.
        runner = poller._default_runner
        assert callable(runner)


# ---------------------------------------------------------------------------
# Watermark file helpers — per-clone state on disk
# ---------------------------------------------------------------------------
# Phase 1-3 took --watermark from the CLI. Real callers (Stop hook, cron)
# need persistent state so they don't re-fire entries on every invocation.
# Watermark file lives at .iteration/.poller-watermark (gitignored — it's
# per-clone consumption progress, not shared state).

class TestReadWatermarkFile:
    def test_missing_file_returns_zero(self, poller, tmp_path):
        """First-ever run on a fresh clone: file doesn't exist. Default
        to 0 (consume everything in the log). NOT an error — that would
        trip the Stop hook on every install."""
        path = tmp_path / "watermark"
        assert poller.read_watermark_file(str(path)) == 0

    def test_empty_file_returns_zero(self, poller, tmp_path):
        """Truncated/empty file is the same case as missing. Defensive
        against a partial write the next tick will overwrite anyway."""
        path = tmp_path / "watermark"
        path.write_text("")
        assert poller.read_watermark_file(str(path)) == 0

    def test_whitespace_only_returns_zero(self, poller, tmp_path):
        """A trailing newline shouldn't trip parsing."""
        path = tmp_path / "watermark"
        path.write_text("   \n  \n")
        assert poller.read_watermark_file(str(path)) == 0

    def test_reads_integer_value(self, poller, tmp_path):
        path = tmp_path / "watermark"
        path.write_text("42\n")
        assert poller.read_watermark_file(str(path)) == 42

    def test_invalid_content_raises(self, poller, tmp_path):
        """A non-integer in the file is corruption — fail loud rather
        than reset to 0 (which would replay the entire log)."""
        path = tmp_path / "watermark"
        path.write_text("not a number")
        with pytest.raises(ValueError, match="watermark"):
            poller.read_watermark_file(str(path))

    def test_negative_value_raises(self, poller, tmp_path):
        """event_ids are non-negative; a negative watermark is corruption."""
        path = tmp_path / "watermark"
        path.write_text("-5")
        with pytest.raises(ValueError, match=r"(?i)watermark"):
            poller.read_watermark_file(str(path))


class TestWriteWatermarkFile:
    def test_writes_integer_value(self, poller, tmp_path):
        path = tmp_path / "watermark"
        poller.write_watermark_file(str(path), 7)
        assert path.read_text().strip() == "7"

    def test_creates_parent_dir(self, poller, tmp_path):
        """Stop hook may run before any other auto-routines tooling has
        touched .iteration/. Don't fail on missing dir."""
        path = tmp_path / "nested" / "dir" / "watermark"
        poller.write_watermark_file(str(path), 3)
        assert path.exists()
        assert path.read_text().strip() == "3"

    def test_overwrites_existing_value(self, poller, tmp_path):
        path = tmp_path / "watermark"
        path.write_text("1")
        poller.write_watermark_file(str(path), 99)
        assert path.read_text().strip() == "99"

    def test_atomic_write_uses_tempfile(self, poller, tmp_path, monkeypatch):
        """A crash mid-write must not leave a corrupt watermark (which
        would replay or skip routines silently). Atomic = tmpfile +
        rename. Verify by checking that the tmpfile pattern .tmp
        appears via os.rename or pathlib.replace flow."""
        # Easier check: the source uses pathlib.replace or os.replace,
        # both of which are atomic on POSIX.
        import inspect
        src = inspect.getsource(poller.write_watermark_file)
        assert ".replace(" in src or "os.replace" in src, (
            "watermark write must be atomic (tmpfile + replace)"
        )


# ---------------------------------------------------------------------------
# poll subcommand — orchestrates watermark read + fire + watermark write
# ---------------------------------------------------------------------------

class TestPollSubcommand:
    def test_poll_reads_watermark_then_fires(self, poller, tmp_path):
        """Watermark file says 1; log has entries 1 and 2; only entry 2
        fires (entry 1 is already consumed)."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
            + json.dumps({"event_id": 2, "routine_id": "b",
                          "ts": "2026-05-10T17:04:00-0700", "sha": "y"}) + "\n"
        )
        wm = tmp_path / "watermark"
        wm.write_text("1")
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["poll", "--log", str(log), "--watermark-file", str(wm)],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert len(runner.calls) == 1
        assert runner.calls[0]["cmd"][runner.calls[0]["cmd"].index("--skill") + 1] == "b"

    def test_poll_persists_new_watermark(self, poller, tmp_path):
        """After firing, new watermark = max(event_id) of consumed
        entries. File on disk reflects it for the next invocation."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
            + json.dumps({"event_id": 5, "routine_id": "b",
                          "ts": "2026-05-10T17:04:00-0700", "sha": "y"}) + "\n"
        )
        wm = tmp_path / "watermark"
        # No initial file → starts at 0
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["poll", "--log", str(log), "--watermark-file", str(wm)],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert wm.exists()
        assert wm.read_text().strip() == "5"

    def test_poll_persists_watermark_even_on_failure(self, poller, tmp_path):
        """Same policy as fire: don't infinite-retry broken routines.
        Watermark advances regardless of subprocess outcome."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 3, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
        )
        wm = tmp_path / "watermark"
        runner = FakeRunner(exit_codes=[1])  # failure
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["poll", "--log", str(log), "--watermark-file", str(wm)],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc != 0, "rc must surface the routine failure"
        assert wm.read_text().strip() == "3", (
            "watermark must advance even when fire failed"
        )

    def test_poll_dry_run_does_not_persist(self, poller, tmp_path):
        """--dry-run lets operators preview what would fire without
        consuming the log. Watermark file MUST be untouched."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 7, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
        )
        wm = tmp_path / "watermark"
        wm.write_text("0")
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["poll", "--log", str(log), "--watermark-file", str(wm), "--dry-run"],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert runner.calls == [], "dry-run must not invoke runner"
        assert wm.read_text().strip() == "0", (
            "dry-run must not persist a new watermark"
        )

    def test_poll_no_pending_does_not_change_watermark_file(self, poller, tmp_path):
        """Empty pending → no fires, no change to watermark file (it
        would re-write the same value, which is a wasted disk op + a
        spurious mtime bump that confuses inotify-style watchers)."""
        log = tmp_path / "log.jsonl"
        log.write_text(
            json.dumps({"event_id": 1, "routine_id": "a",
                        "ts": "2026-05-10T17:03:00-0700", "sha": "x"}) + "\n"
        )
        wm = tmp_path / "watermark"
        wm.write_text("1")
        original_mtime = wm.stat().st_mtime_ns
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["poll", "--log", str(log), "--watermark-file", str(wm)],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0
        assert runner.calls == []
        # Same value, so file should not have been touched
        assert wm.stat().st_mtime_ns == original_mtime, (
            "no-op poll must not bump watermark file mtime"
        )

    def test_poll_missing_log_is_clean_exit(self, poller, tmp_path):
        """Fresh install: no log file yet. Exit 0, no fires, watermark
        unchanged."""
        wm = tmp_path / "watermark"
        runner = FakeRunner()
        out, err = io.StringIO(), io.StringIO()
        rc = poller.cli_main(
            ["poll", "--log", str(tmp_path / "nope.jsonl"),
             "--watermark-file", str(wm)],
            stdout=out, stderr=err, runner=runner,
        )
        assert rc == 0, err.getvalue()
        assert runner.calls == []
        # No fires happened, so no watermark file was created
        assert not wm.exists()
