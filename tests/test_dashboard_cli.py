"""
Tests for dashboard's CLI shim (PRD #10 Module 4, phase 2).

Contract:
    python scripts/dashboard.py sync \
        --config <path> --state <path> --log <path> \
        --repo owner/name --iter <N> [--now "2026-05-10T14:00:00-0700"]

Composes: config + state + log → render_dashboard → sync_dashboard.
The GHA workflow calls this after `orchestrator.py tick` writes the
fresh state. Output (JSON on stdout) is what the workflow logs / surfaces.

Tests inject a fake gh_run so we exercise the full pipeline without hitting
the network. The pure render + pure sync_dashboard are tested elsewhere;
this file pins that the *composition* + the CLI surface (flag names,
file formats, exit codes) hold.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_dashboard():
    spec = importlib.util.spec_from_file_location(
        "dashboard", ROOT / "scripts" / "dashboard.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dash():
    return _load_dashboard()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _v4_config():
    return {
        "schema_version": 4,
        "repo_slug": "owner/repo",
        "goal": "test",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": []},
        "routines": [
            {
                "id": "r1",
                "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "*/30 * * * *", "human": "every 30 minutes"},
                "purpose": "test",
                "automation_level": "auto",
                "execution_surface": "gha",
                "est_minutes": 5,
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9 AM",
            "anti_flap_window": 7,
            "default_stagnation_threshold": 7,
            "process_evolve_requests": True,
            "idle_window": "always",
            "gha_minutes_cap": 60,
            "kill_switch": False,
        },
    }


def _v1_state():
    return {
        "schema_version": 1,
        "gha_minutes_used_today": 0,
        "gha_minutes_reset_date": "2026-05-10",
        "last_event_id": 1,
        "kill_switch_active": False,
        "last_dispatch": {},
    }


@pytest.fixture
def cfg_path(tmp_path):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(_v4_config()))
    return p


@pytest.fixture
def state_path(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(_v1_state()))
    return p


@pytest.fixture
def log_path(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text("")
    return p


class FakeGh:
    """Same shape as the FakeGh in test_dashboard_sync.py — recorded
    callable, queued responses, snapshots --body-file content."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.body_files: dict[str, str] = {}
        self._responses: list[tuple[tuple[str, ...], str]] = []

    def add_response(self, prefix: list[str], stdout: str) -> None:
        self._responses.append((tuple(prefix), stdout))

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        for i, a in enumerate(args):
            if a == "--body-file" and i + 1 < len(args):
                fp = args[i + 1]
                try:
                    self.body_files[fp] = Path(fp).read_text()
                except OSError:
                    pass
        for prefix, out in self._responses:
            if tuple(args[: len(prefix)]) == prefix:
                return out
        return ""


# ---------------------------------------------------------------------------
# Happy path: create + update
# ---------------------------------------------------------------------------

class TestSyncCreate:
    def test_creates_when_no_existing_issue(
        self, dash, cfg_path, state_path, log_path
    ):
        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/42\n",
        )
        out = io.StringIO()
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "owner/repo",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
            gh_run=gh,
        )
        assert rc == 0
        payload = json.loads(out.getvalue())
        assert payload["action"] == "created"
        assert payload["issue_number"] == 42
        # Body file should have been the rendered dashboard with the marker
        create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        body_arg = next(
            (c for i, c in enumerate(create_call)
             if i > 0 and create_call[i - 1] == "--body-file"),
            None,
        )
        assert body_arg is not None
        body = gh.body_files[body_arg]
        assert dash.DASHBOARD_MARKER in body
        # Title should mention iter 7
        title_arg = next(
            (c for i, c in enumerate(create_call)
             if i > 0 and create_call[i - 1] == "--title"),
            None,
        )
        assert "7" in (title_arg or "")


class TestSyncUpdate:
    def test_updates_when_body_differs(
        self, dash, cfg_path, state_path, log_path
    ):
        # First, render what we'd produce so we can build a "different"
        # existing body.
        import yaml
        import datetime as dt
        from zoneinfo import ZoneInfo
        cfg = yaml.safe_load(cfg_path.read_text())
        state = json.loads(state_path.read_text())
        new_body = dash.render_dashboard(
            state, cfg, [],
            now=dt.datetime(2026, 5, 10, 14, 0, tzinfo=ZoneInfo("UTC")),
        )
        existing_body = (
            f"# auto-routines dashboard — iter 7\n\nOLD\n\n{dash.DASHBOARD_MARKER}\n"
        )
        assert existing_body != new_body

        gh = FakeGh()
        gh.add_response(["issue", "list"], json.dumps([{
            "number": 42,
            "title": "auto-routines dashboard — iter 7",
            "url": "https://github.com/owner/repo/issues/42",
            "body": existing_body,
        }]))
        gh.add_response(["issue", "edit"], "")

        out = io.StringIO()
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "owner/repo",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
            gh_run=gh,
        )
        assert rc == 0
        payload = json.loads(out.getvalue())
        assert payload["action"] == "updated"
        assert payload["issue_number"] == 42


class TestSyncUnchanged:
    def test_unchanged_when_body_matches(
        self, dash, cfg_path, state_path, log_path
    ):
        # Render new body, present it as the existing body — should noop.
        import yaml
        import datetime as dt
        from zoneinfo import ZoneInfo
        cfg = yaml.safe_load(cfg_path.read_text())
        state = json.loads(state_path.read_text())
        body = dash.render_dashboard(
            state, cfg, [],
            now=dt.datetime(2026, 5, 10, 14, 0, tzinfo=ZoneInfo("UTC")),
        )

        gh = FakeGh()
        gh.add_response(["issue", "list"], json.dumps([{
            "number": 42,
            "title": "auto-routines dashboard — iter 7",
            "url": "https://github.com/owner/repo/issues/42",
            "body": body,
        }]))
        out = io.StringIO()
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "owner/repo",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
            gh_run=gh,
        )
        assert rc == 0
        payload = json.loads(out.getvalue())
        assert payload["action"] == "unchanged"
        # Critically: no edit call (no notification spam)
        assert not any(c[:2] == ["issue", "edit"] for c in gh.calls)


# ---------------------------------------------------------------------------
# Log file handling
# ---------------------------------------------------------------------------

class TestLogIngest:
    def test_log_jsonl_entries_appear_in_dashboard(
        self, dash, cfg_path, state_path, tmp_path
    ):
        # Write a couple of JSONL log entries
        log_path = tmp_path / "log.jsonl"
        log_path.write_text(
            json.dumps({
                "ts": "2026-05-10T13:00:00-0700",
                "iter": 7,
                "task": "r1",
                "outcome": "ok",
                "summary": "test ran",
            }) + "\n"
        )

        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/42\n",
        )
        out = io.StringIO()
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "owner/repo",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
            gh_run=gh,
        )
        assert rc == 0
        # The created body should reference our log entry
        create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        body_arg = next(
            (c for i, c in enumerate(create_call)
             if i > 0 and create_call[i - 1] == "--body-file"),
            None,
        )
        body = gh.body_files[body_arg]
        assert "test ran" in body or "r1" in body

    def test_missing_log_treated_as_empty(
        self, dash, cfg_path, state_path, tmp_path
    ):
        """A fresh repo has no log yet. Don't crash; render with no
        activity rows."""
        nonexistent = tmp_path / "no-such-log.jsonl"
        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/42\n",
        )
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(nonexistent),
                "--repo", "owner/repo",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=io.StringIO(),
            gh_run=gh,
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Errors / ergonomics
# ---------------------------------------------------------------------------

class TestErrors:
    def test_missing_config_returns_nonzero(
        self, dash, tmp_path, state_path, log_path
    ):
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(tmp_path / "nope.yaml"),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "owner/repo",
                "--iter", "7",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            gh_run=FakeGh(),
        )
        assert rc != 0

    def test_now_must_be_tz_aware(
        self, dash, cfg_path, state_path, log_path
    ):
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "owner/repo",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00",  # naive
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            gh_run=FakeGh(),
        )
        assert rc != 0

    def test_empty_repo_returns_nonzero(
        self, dash, cfg_path, state_path, log_path
    ):
        rc = dash.cli_main(
            [
                "sync",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--log", str(log_path),
                "--repo", "",
                "--iter", "7",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            gh_run=FakeGh(),
        )
        assert rc != 0


# ---------------------------------------------------------------------------
# Subprocess smoke
# ---------------------------------------------------------------------------

class TestSubprocessSmoke:
    def test_help_runs(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dashboard.py"), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "sync" in result.stdout

    def test_sync_help_runs(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dashboard.py"), "sync", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "--config" in result.stdout
        assert "--state" in result.stdout
        assert "--repo" in result.stdout
        assert "--iter" in result.stdout
