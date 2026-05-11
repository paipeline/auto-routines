"""
Drift detectors for issue #82 — live iteration monitor flags on
`scripts/status.py`.

Adds three flags to `scripts/status.py`:

1. `--watch [N]` — refresh the render every N seconds (default 5).
   The existing `tests/test_status.py::test_status_does_not_invoke_claude`
   forbids `subprocess` and `os.system`, so `--watch` uses ANSI escape
   sequences (`\\033[2J\\033[H`) instead of `os.system("clear")` — the
   spec wording was a suggestion, the locality contract is the
   ground truth.
2. `--since <duration>` — filter the activity tail to fires whose
   `ts` is within the given duration (`30m`, `1h`, `7d`, `90s`).
   Malformed values exit with `rc=2` and a clear error.
3. `--routine <id>` — already existed, but issue #82 demands:
   - last 20 fires (was 10), with summary and PR URL when present;
   - unknown id error message lists valid ids.

Plus a drift detector: every flag in `SKILL.md > Mode: status` must
match the argparse parser (catches doc drift in both directions).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from .conftest import ROOT, status


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_status.py)
# ---------------------------------------------------------------------------

def _write_repo(tmp_path: Path, config: dict, log_lines: list[dict] | None = None) -> Path:
    iter_dir = tmp_path / ".iteration"
    iter_dir.mkdir()
    (iter_dir / "config.yaml").write_text(yaml.safe_dump(config))
    if log_lines is not None:
        (iter_dir / "log.jsonl").write_text(
            "\n".join(json.dumps(e) for e in log_lines) + "\n"
        )
    return tmp_path


def _base_config() -> dict:
    return {
        "schema_version": 4,
        "repo_slug": "demo",
        "goal": "ship v1",
        "mode": "goal-driven",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": [
            {
                "id": "prd-implement",
                "primitive": "scheduled",
                "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
                "purpose": "drive PRD",
                "automation_level": "auto",
                "state": "ACTIVE",
                "self_evolve": True,
                "stagnation_threshold": 5,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
            },
            {
                "id": "daily-digest",
                "primitive": "scheduled",
                "trigger": {"cron": "0 18 * * *", "human": "6:00 PM daily"},
                "purpose": "summarize the day",
                "automation_level": "auto",
                "state": "STOPPED",
                "self_evolve": False,
                "stagnation_threshold": 14,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 3,
            "default_stagnation_threshold": 5,
            "budget": "medium",
        },
    }


# ---------------------------------------------------------------------------
# --since: duration parser
# ---------------------------------------------------------------------------

class TestParseDuration:
    """`status.parse_duration(s) -> int seconds` must accept the
    documented forms and reject ambiguous junk. Without this, the
    --since flag's contract is whatever the implementation drifts
    into."""

    def test_seconds(self):
        assert status.parse_duration("30s") == 30
        assert status.parse_duration("90s") == 90

    def test_minutes(self):
        assert status.parse_duration("1m") == 60
        assert status.parse_duration("30m") == 30 * 60

    def test_hours(self):
        assert status.parse_duration("1h") == 3600
        assert status.parse_duration("24h") == 24 * 3600

    def test_days(self):
        assert status.parse_duration("1d") == 86_400
        assert status.parse_duration("7d") == 7 * 86_400

    def test_rejects_bare_int(self):
        # Bare integers are ambiguous (seconds? minutes?) — reject
        # so the CLI never silently picks the wrong unit.
        with pytest.raises(ValueError):
            status.parse_duration("60")

    def test_rejects_unknown_unit(self):
        with pytest.raises(ValueError):
            status.parse_duration("5w")  # weeks not supported

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            status.parse_duration("")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            status.parse_duration("notaduration")


class TestSinceFilter:
    """`--since 1h` filters the activity tail to fires within the
    duration. Pure local filter — no clock magic, just compares
    each log entry's `ts` to `now - duration`."""

    def test_main_since_filters_old_fires(self, tmp_path, capsys, monkeypatch):
        cfg = _base_config()
        # One old entry (8 days), one recent (10 minutes). With
        # --since 1h, only the recent entry should be in the
        # detail view.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(days=8)).isoformat()
        recent_ts = (now - timedelta(minutes=10)).isoformat()
        log = [
            {"ts": old_ts, "routine": "prd-implement", "outcome": "ok",
             "summary": "old fire", "increment_signal": True},
            {"ts": recent_ts, "routine": "prd-implement", "outcome": "ok",
             "summary": "recent fire", "increment_signal": True},
        ]
        repo = _write_repo(tmp_path, cfg, log_lines=log)
        rc = status.main([
            "--root", str(repo),
            "--routine", "prd-implement",
            "--since", "1h",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "recent fire" in out
        assert "old fire" not in out, (
            "`--since 1h` must drop entries older than 1 hour from the "
            "drill-in view; old fire leaked through"
        )

    def test_main_since_rejects_malformed_with_error(self, tmp_path, capsys):
        cfg = _base_config()
        repo = _write_repo(tmp_path, cfg)
        rc = status.main([
            "--root", str(repo),
            "--since", "5xyz",
        ])
        assert rc == 2, (
            "Malformed --since values must exit rc=2 (unreadable arg) "
            "with a clear error, not silently fall back to 'no filter'"
        )
        err = capsys.readouterr().err
        assert "since" in err.lower(), (
            "Error message must mention the --since flag so the user "
            f"can fix it; got {err!r}"
        )


# ---------------------------------------------------------------------------
# --watch
# ---------------------------------------------------------------------------

class TestWatch:
    """`--watch [N]` refreshes the render every N seconds. Implemented
    with ANSI clear-screen escape (`\\033[2J\\033[H`) + `time.sleep`,
    NOT `os.system("clear")` — the locality contract test in
    `tests/test_status.py` forbids `os.system`."""

    def test_watch_flag_exists_in_parser(self):
        import argparse
        parser = status._build_parser()
        assert isinstance(parser, argparse.ArgumentParser), (
            "status.py must expose its argparse parser via "
            "`_build_parser()` so tests + the SKILL.md drift detector "
            "can introspect the flag set"
        )
        # Parse `--watch 3` — should succeed with watch=3.
        args = parser.parse_args(["--watch", "3"])
        assert args.watch == 3

    def test_watch_default_interval(self):
        parser = status._build_parser()
        # Bare `--watch` (no value) defaults to 5 seconds per spec.
        args = parser.parse_args(["--watch"])
        assert args.watch == 5, (
            f"--watch with no value must default to 5 seconds; "
            f"got {args.watch!r}"
        )

    def test_watch_loop_redraws_with_ansi_clear(self, tmp_path, capsys, monkeypatch):
        """Drive one tick of the watch loop and confirm:
        - the ANSI clear-screen escape was emitted before the render;
        - the render output was emitted;
        - `time.sleep` was called with the configured interval;
        - Ctrl-C exits cleanly (rc=0)."""
        cfg = _base_config()
        repo = _write_repo(tmp_path, cfg)

        sleep_calls: list[float] = []

        def fake_sleep(n):
            sleep_calls.append(n)
            # After the first tick, raise KeyboardInterrupt to exit
            # the loop cleanly.
            raise KeyboardInterrupt

        monkeypatch.setattr(status.time, "sleep", fake_sleep)

        rc = status.main(["--root", str(repo), "--watch", "3"])
        assert rc == 0, (
            "--watch must exit rc=0 on Ctrl-C, not propagate the "
            "KeyboardInterrupt as an error"
        )
        out = capsys.readouterr().out
        # ANSI clear-screen + home cursor sequence.
        assert "\033[2J" in out and "\033[H" in out, (
            "--watch must emit the ANSI clear-screen escape "
            "(\\033[2J\\033[H) — `os.system('clear')` is forbidden "
            "by the locality contract test"
        )
        # The render itself must be in the output.
        assert "goal:" in out and "prd-implement" in out
        # Sleep called with the configured interval.
        assert sleep_calls == [3], (
            f"--watch must call time.sleep with the configured "
            f"interval; got {sleep_calls!r}"
        )


# ---------------------------------------------------------------------------
# --routine drill-in upgrades
# ---------------------------------------------------------------------------

class TestRoutineDrillIn:
    """Issue #82 upgrades `--routine` from `last 10 fires` to
    `last 20 fires + PR URL when present + helpful error listing
    valid ids on unknown id`."""

    def test_unknown_routine_lists_valid_ids(self, tmp_path, capsys):
        cfg = _base_config()
        repo = _write_repo(tmp_path, cfg)
        rc = status.main(["--root", str(repo), "--routine", "nope"])
        assert rc == 1
        err = capsys.readouterr().err
        # The valid ids must be in the error so the user can
        # immediately correct the typo.
        assert "prd-implement" in err and "daily-digest" in err, (
            "Unknown --routine error must list the valid routine "
            "ids so the user can correct the typo without re-running"
            f"; got: {err!r}"
        )

    def test_drill_in_shows_last_20_fires(self, tmp_path, capsys):
        cfg = _base_config()
        # 25 entries — drill-in must show the last 20, not 10.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        log = [
            {
                "ts": (now - timedelta(minutes=25 - i)).isoformat(),
                "routine": "prd-implement",
                "outcome": "ok",
                # Use a token-shaped summary so substring matches
                "summary": f"fire-{i:03d}",
                "increment_signal": True,
            }
            for i in range(25)
        ]
        repo = _write_repo(tmp_path, cfg, log_lines=log)
        rc = status.main(["--root", str(repo), "--routine", "prd-implement"])
        assert rc == 0
        out = capsys.readouterr().out
        # fires 005..024 should be present (last 20). 000..004 dropped.
        present = sum(1 for i in range(25) if f"fire-{i:03d}" in out)
        assert present == 20, (
            f"Drill-in must show the last 20 fires (spec issue #82); "
            f"got {present}. Without this, fast-firing routines like "
            f"commit-tests show too narrow a window."
        )
        assert "fire-024" in out, "Most recent fire must be present"
        assert "fire-000" not in out, "Oldest fire must be dropped"

    def test_drill_in_shows_pr_url_when_present(self, tmp_path, capsys):
        cfg = _base_config()
        log = [
            {"ts": "2026-05-09T10:00:00+0200", "routine": "prd-implement",
             "outcome": "ok", "summary": "shipped slice",
             "pr_url": "https://github.com/owner/repo/pull/42",
             "increment_signal": True},
        ]
        repo = _write_repo(tmp_path, cfg, log_lines=log)
        rc = status.main(["--root", str(repo), "--routine", "prd-implement"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "https://github.com/owner/repo/pull/42" in out, (
            "Drill-in must surface `pr_url` from log entries — the "
            "spec calls it out explicitly so a user can jump to the "
            "PR from the status view"
        )


# ---------------------------------------------------------------------------
# Flag composition
# ---------------------------------------------------------------------------

class TestFlagComposition:
    """The flags must compose without conflicts. The interesting
    combos: `--watch + --since`, `--watch + --routine`,
    `--routine + --since` (already tested above)."""

    def test_watch_with_since_parses(self):
        parser = status._build_parser()
        args = parser.parse_args(["--watch", "2", "--since", "1h"])
        assert args.watch == 2
        assert args.since == "1h"

    def test_watch_with_routine_parses(self):
        parser = status._build_parser()
        args = parser.parse_args(["--watch", "2", "--routine", "prd-implement"])
        assert args.watch == 2
        assert args.routine == "prd-implement"


# ---------------------------------------------------------------------------
# SKILL.md drift detector
# ---------------------------------------------------------------------------

class TestSkillMdDocDrift:
    """The SKILL.md `Mode: status` section documents each flag. If
    the doc and the parser drift apart — flag added to code but not
    doc, or vice versa — users hit confusion. Pin both directions."""

    SKILL_PATH = ROOT / "SKILL.md"

    def _mode_status_section(self) -> str:
        text = self.SKILL_PATH.read_text()
        # Extract the `## Mode: status` section: from that heading
        # to the next `## ` heading.
        m = re.search(
            r"^##\s+Mode:\s*`?status`?\s*$(.*?)(?=^##\s)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert m is not None, (
            "SKILL.md must have a `## Mode: status` (or "
            "`## Mode: `status``) section so users know how to invoke "
            "the status mode"
        )
        return m.group(1)

    def test_skill_md_documents_watch(self):
        section = self._mode_status_section()
        assert "--watch" in section, (
            "SKILL.md `Mode: status` section must document `--watch` "
            "(issue #82 acceptance criterion). Without it, users "
            "don't know the flag exists."
        )

    def test_skill_md_documents_since(self):
        section = self._mode_status_section()
        assert "--since" in section, (
            "SKILL.md `Mode: status` section must document `--since` "
            "(issue #82 acceptance criterion). Without it, users "
            "don't know the flag exists."
        )

    def test_skill_md_documents_routine(self):
        section = self._mode_status_section()
        assert "--routine" in section, (
            "SKILL.md `Mode: status` section must document `--routine`"
        )

    def test_doc_flags_match_parser(self):
        """Bidirectional check: every flag in SKILL.md's `Mode:
        status` section must be in the parser; every flag in the
        parser must be in the section. Catches drift in either
        direction."""
        section = self._mode_status_section()
        # Pull flags out of code blocks in the section — match
        # tokens starting with `--`.
        doc_flags = set(re.findall(r"--[a-z][a-z0-9-]*", section))

        parser = status._build_parser()
        parser_flags: set[str] = set()
        for action in parser._actions:
            for opt in action.option_strings:
                if opt.startswith("--"):
                    parser_flags.add(opt)

        # Exclude --help (every argparse parser has it; rarely
        # called out in user-facing docs).
        parser_flags.discard("--help")

        # Direction 1: every doc flag must exist in parser.
        doc_unknown = doc_flags - parser_flags
        assert not doc_unknown, (
            f"SKILL.md `Mode: status` documents flags that don't "
            f"exist in status.py's parser: {sorted(doc_unknown)}. "
            f"Doc-drift fails CI per issue #82 acceptance criterion."
        )

        # Direction 2: every parser flag must be documented. Allow
        # an explicit allow-list for internal flags by name.
        INTERNAL = {"--root"}  # implementation detail, not user-facing
        parser_undocumented = parser_flags - doc_flags - INTERNAL
        assert not parser_undocumented, (
            f"status.py exposes flags that SKILL.md does NOT "
            f"document: {sorted(parser_undocumented)}. Doc-drift "
            f"fails CI per issue #82 acceptance criterion. If a flag "
            f"is intentionally internal, add it to the INTERNAL set "
            f"in this test with a comment explaining why."
        )
