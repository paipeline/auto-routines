"""
Tests for orchestrator's CLI shim (PRD #10 Module 4, phase 1).

Contract:
    python scripts/orchestrator.py tick \
        --config <path> --state <path> \
        --trigger-type cron --cron-expr "*/30 * * * *" \
        [--now "2026-05-10T14:00:00-0700"]

The CLI is a thin glue layer the GHA workflow calls. It composes:
    config + state → match_trigger → tick → write state → emit decisions

Two ways the workflow consumes the result:
  1. Reads stdout (single-line JSON: {"decisions": [...]}) to decide
     what to actually execute (a `gh workflow run` for gha-surface
     decisions, a repository_dispatch for local).
  2. Reads the rewritten state.json to commit it back to the repo.

Tests exercise `cli_main(argv, ...)` directly — calling Python from
Python is faster and gives us injectable `now` and stdout. A subprocess
smoke test pins that the file is actually executable too.

Why TDD this layer at all? The pure functions are well-tested. The CLI
is the only place the integration shape (flag names, file format, exit
codes) gets pinned. If a flag renames silently the workflow breaks
overnight; this is the fence around that.
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


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location(
        "orchestrator", ROOT / "scripts" / "orchestrator.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return _load_orchestrator()


# ---------------------------------------------------------------------------
# Fixtures — minimal valid config + state on disk
# ---------------------------------------------------------------------------

def _v4_config(routines=None):
    return {
        "schema_version": 4,
        "repo_slug": "demo",
        "goal": "test",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": []},
        "routines": routines or [],
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
        "last_event_id": 0,
        "kill_switch_active": False,
        "last_dispatch": {},
    }


def _scheduled_routine(rid="r1", cron="*/30 * * * *", surface="gha"):
    return {
        "id": rid,
        "state": "ACTIVE",
        "primitive": "scheduled",
        "trigger": {"cron": cron, "human": f"every {cron}"},
        "purpose": "test",
        "automation_level": "auto",
        "execution_surface": surface,
        "est_minutes": 5,
    }


@pytest.fixture
def cfg_path(tmp_path):
    """Write a v4 config with one scheduled gha routine and return path."""
    import yaml
    cfg = _v4_config([_scheduled_routine()])
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


@pytest.fixture
def state_path(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(_v1_state()))
    return p


# ---------------------------------------------------------------------------
# Happy path — cron trigger fires a scheduled routine
# ---------------------------------------------------------------------------

class TestTickCron:
    def test_emits_decisions_json_on_stdout(self, orch, cfg_path, state_path):
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        payload = json.loads(out.getvalue())
        assert "decisions" in payload
        decisions = payload["decisions"]
        assert len(decisions) == 1
        assert decisions[0]["routine_id"] == "r1"
        assert decisions[0]["action"] == "fire"
        assert decisions[0]["surface"] == "gha"

    def test_writes_updated_state_back(self, orch, cfg_path, state_path):
        orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=io.StringIO(),
        )
        new_state = json.loads(state_path.read_text())
        # Cost cap consumed by the fire
        assert new_state["gha_minutes_used_today"] == 5
        # Event id ticked
        assert new_state["last_event_id"] == 1
        # last_dispatch ledger gained the routine
        assert "r1" in new_state["last_dispatch"]
        assert new_state["last_dispatch"]["r1"]["surface"] == "gha"

    def test_no_match_emits_empty_decisions(self, orch, cfg_path, state_path):
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "0 9 * * *",  # No routine matches this
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        payload = json.loads(out.getvalue())
        assert payload["decisions"] == []
        # State still rewritten — at minimum the event_id ticks even when
        # nothing fires (so the dashboard heartbeat advances).
        new_state = json.loads(state_path.read_text())
        assert new_state["last_event_id"] == 1


# ---------------------------------------------------------------------------
# Other trigger types
# ---------------------------------------------------------------------------

class TestTickHook:
    def test_hook_trigger_fires_matching_hook_routine(self, orch, tmp_path, state_path):
        import yaml
        cfg = _v4_config([
            {
                "id": "h1",
                "state": "ACTIVE",
                "primitive": "hook",
                "trigger": {"event": "Stop"},
                "purpose": "x",
                "automation_level": "auto",
            },
        ])
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "hook",
                "--hook-event", "Stop",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        decisions = json.loads(out.getvalue())["decisions"]
        assert decisions[0]["routine_id"] == "h1"
        assert decisions[0]["action"] == "fire"
        assert decisions[0]["surface"] == "local"  # hook is always local


class TestTickGitHookChangedFiles:
    """PRD #10 priority rule 4: the git-hook trigger accepts an optional
    `--changed-files` flag that the GHA workflow / local post-commit hook
    populates from the diff. When supplied, routines that subscribe to
    matching paths via `path_filters` priority-elevate themselves —
    ONLY they fire on this tick. This is what implements
    `goal.md changed → meta-evolve fires alone`.
    """

    @staticmethod
    def _git_hook_routine(rid: str, *, path_filters: list[str] | None = None) -> dict:
        r = {
            "id": rid,
            "state": "ACTIVE",
            "primitive": "git-hook",
            "trigger": {},
            "purpose": "test",
            "automation_level": "auto",
        }
        if path_filters is not None:
            r["path_filters"] = path_filters
        return r

    def test_changed_files_flag_priority_elevates_path_filtered_routine(
        self, orch, tmp_path, state_path
    ):
        import yaml
        cfg = _v4_config([
            self._git_hook_routine("commit-tests"),
            self._git_hook_routine("commit-lint"),
            self._git_hook_routine(
                "meta-evolve", path_filters=[".iteration/goal.md"]
            ),
        ])
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "git-hook",
                "--changed-files", ".iteration/goal.md,scripts/orchestrator.py",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        decisions = json.loads(out.getvalue())["decisions"]
        assert [d["routine_id"] for d in decisions] == ["meta-evolve"]
        assert decisions[0]["action"] == "fire"

    def test_no_changed_files_flag_falls_back_to_all_git_hooks(
        self, orch, tmp_path, state_path
    ):
        """Backward-compat: callers that don't supply --changed-files
        still see every git-hook routine fire (legacy behavior)."""
        import yaml
        cfg = _v4_config([
            self._git_hook_routine("commit-tests"),
            self._git_hook_routine(
                "meta-evolve", path_filters=[".iteration/goal.md"]
            ),
        ])
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "git-hook",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        decisions = json.loads(out.getvalue())["decisions"]
        assert {d["routine_id"] for d in decisions} == {
            "commit-tests", "meta-evolve",
        }

    def test_changed_files_supports_newline_separator(
        self, orch, tmp_path, state_path
    ):
        """The post-commit hook is more likely to pass `\\n`-separated
        output (e.g. `git diff-tree --name-only HEAD`) than to join with
        commas. Both shapes parse equivalently."""
        import yaml
        cfg = _v4_config([
            self._git_hook_routine("commit-tests"),
            self._git_hook_routine(
                "meta-evolve", path_filters=[".iteration/goal.md"]
            ),
        ])
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "git-hook",
                "--changed-files", ".iteration/goal.md\nREADME.md",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        decisions = json.loads(out.getvalue())["decisions"]
        assert [d["routine_id"] for d in decisions] == ["meta-evolve"]


class TestTickManual:
    def test_manual_trigger_with_routine_ids(self, orch, tmp_path, state_path):
        import yaml
        cfg = _v4_config([
            _scheduled_routine("a"),
            _scheduled_routine("b"),
        ])
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "manual",
                "--routine-ids", "b",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        decisions = json.loads(out.getvalue())["decisions"]
        assert [d["routine_id"] for d in decisions] == ["b"]

    def test_manual_routine_ids_comma_separated(self, orch, tmp_path, state_path):
        import yaml
        cfg = _v4_config([
            _scheduled_routine("a"),
            _scheduled_routine("b"),
            _scheduled_routine("c"),
        ])
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "manual",
                "--routine-ids", "a,c",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        decisions = json.loads(out.getvalue())["decisions"]
        assert [d["routine_id"] for d in decisions] == ["a", "c"]


# ---------------------------------------------------------------------------
# State integrity — kill switch propagates, cap blocks, etc.
# ---------------------------------------------------------------------------

class TestStateIntegrity:
    def test_kill_switch_in_config_blocks_dispatch(
        self, orch, tmp_path, state_path
    ):
        import yaml
        cfg = _v4_config([_scheduled_routine()])
        cfg["meta"]["kill_switch"] = True
        cp = tmp_path / "c.yaml"
        cp.write_text(yaml.safe_dump(cfg))
        out = io.StringIO()
        orch.cli_main(
            [
                "tick",
                "--config", str(cp),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        decisions = json.loads(out.getvalue())["decisions"]
        assert decisions[0]["action"] == "skip"
        assert "kill switch" in decisions[0]["reason"]

    def test_state_file_unchanged_when_decision_pure(
        self, orch, cfg_path, state_path
    ):
        """Even when nothing fires, the state file is rewritten with the
        new event_id. The CLI is not lying-by-omission about state."""
        before_mtime = state_path.stat().st_mtime
        orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "0 9 * * *",  # no match
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=io.StringIO(),
        )
        # File contents must reflect the new event_id even on no-match
        new_state = json.loads(state_path.read_text())
        assert new_state["last_event_id"] == 1


# ---------------------------------------------------------------------------
# CLI ergonomics — bad flags, missing files, naive `now`
# ---------------------------------------------------------------------------

class TestCliErrors:
    def test_missing_config_returns_nonzero(
        self, orch, tmp_path, state_path
    ):
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(tmp_path / "nonexistent.yaml"),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0

    def test_unknown_trigger_type_returns_nonzero(
        self, orch, cfg_path, state_path
    ):
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "lunar-eclipse",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0

    def test_now_must_be_tz_aware(self, orch, cfg_path, state_path):
        """A naive --now is the silent-UTC footgun PRD #10 review called
        out. Refuse loudly."""
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
                "--now", "2026-05-10T14:00:00",  # no offset
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0

    def test_creates_initial_state_if_missing(
        self, orch, cfg_path, tmp_path
    ):
        """First-tick scenario: state.json doesn't exist yet. Workflow
        shouldn't crash on first run. The CLI should bootstrap it."""
        sp = tmp_path / "state.json"  # doesn't exist
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "tick",
                "--config", str(cfg_path),
                "--state", str(sp),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            stdout=out,
        )
        assert rc == 0
        assert sp.exists()
        new_state = json.loads(sp.read_text())
        assert new_state["schema_version"] == 1
        assert new_state["last_event_id"] == 1


# ---------------------------------------------------------------------------
# Subprocess smoke — file is executable as `python scripts/orchestrator.py`
# ---------------------------------------------------------------------------

class TestSubprocessSmoke:
    def test_help_runs_without_crash(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "orchestrator.py"), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "tick" in result.stdout

    def test_tick_help_runs(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "orchestrator.py"), "tick", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "--config" in result.stdout
        assert "--state" in result.stdout
        assert "--trigger-type" in result.stdout

    def test_real_subprocess_tick(self, cfg_path, state_path):
        """Pin that the file actually runs as a script — argparse + I/O
        are real, not mocked. Catches missing shebang or bad imports."""
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "orchestrator.py"),
                "tick",
                "--config", str(cfg_path),
                "--state", str(state_path),
                "--trigger-type", "cron",
                "--cron-expr", "*/30 * * * *",
                "--now", "2026-05-10T14:00:00+0000",
            ],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["decisions"][0]["action"] == "fire"
