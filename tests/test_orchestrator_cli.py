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
# test-fire — manual one-shot dispatch plan for a single routine
#
# goal.md (Skill UX): "/auto-routines test-fire <routine_id> to manually
# fire one routine without waiting for cron — useful for debugging."
#
# Surface: a dry-run subcommand that takes a routine id, validates it, and
# prints the dispatch command the user (or the SKILL.md slash-command
# wrapper) can copy-paste or pipe to a shell. NO state writes (it's a
# manual override, not a real fire). NO subprocess execution (out of
# scope for v1 — the user runs the printed command if they want).
# ---------------------------------------------------------------------------

class TestTestFire:
    def _cfg_with_routines(self, tmp_path, routines):
        import yaml
        cfg = _v4_config(routines)
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg))
        return p

    def test_subcommand_prints_dispatch_plan_for_known_routine(
        self, orch, tmp_path
    ):
        cfg = self._cfg_with_routines(tmp_path, [_scheduled_routine(rid="r1")])
        out = io.StringIO()
        rc = orch.cli_main(
            ["test-fire", "--config", str(cfg), "--routine-id", "r1"],
            stdout=out,
        )
        assert rc == 0, "test-fire on a known routine must succeed"
        text = out.getvalue()
        # The plan must name the routine so the user knows what was matched.
        assert "r1" in text
        # The plan must show the dispatch command the user would run —
        # mirrors the post-commit-hook substitution shape so test-fire
        # output and a real local fire stay congruent.
        assert "claude" in text.lower(), (
            "dispatch plan must include the `claude` invocation so the "
            "user sees what would actually run"
        )
        assert "/r1" in text or "-p \"/r1\"" in text or "-p '/r1'" in text, (
            "dispatch plan must show the slash-command shape (`/<routine_id>`) "
            "so test-fire output mirrors the real dispatch"
        )

    def test_subcommand_errors_on_unknown_routine(self, orch, tmp_path):
        cfg = self._cfg_with_routines(tmp_path, [_scheduled_routine(rid="r1")])
        err = io.StringIO()
        rc = orch.cli_main(
            ["test-fire", "--config", str(cfg), "--routine-id", "does-not-exist"],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc != 0, (
            "test-fire on a missing id must fail loudly — silent success "
            "would let typos go unnoticed"
        )
        assert "does-not-exist" in err.getvalue(), (
            "the error message must name the bad id so the user can fix the typo"
        )

    def test_subcommand_does_not_mutate_state(self, orch, tmp_path):
        """test-fire is a manual-override dry-run: it must NOT write to
        state.json. Otherwise running test-fire eats the cost cap, ticks
        last_event_id, and pollutes last_dispatch — corrupting the real
        orchestrator's accounting."""
        cfg = self._cfg_with_routines(tmp_path, [_scheduled_routine(rid="r1")])
        state_path = tmp_path / "state.json"
        before = json.dumps(_v1_state())
        state_path.write_text(before)
        rc = orch.cli_main(
            ["test-fire", "--config", str(cfg), "--routine-id", "r1"],
            stdout=io.StringIO(),
        )
        assert rc == 0
        # state file untouched (test-fire takes no --state flag at all,
        # but if a future patch tries to wire one in, this still pins
        # the contract: existing state on disk is unchanged).
        assert state_path.read_text() == before, (
            "test-fire must be read-only — state on disk must be byte-identical"
        )

    def test_subcommand_warns_on_stopped_routine_but_still_emits_plan(
        self, orch, tmp_path
    ):
        """A user explicitly typing `test-fire <id>` is asking to fire it
        even if it's STOPPED — that's the whole point of a manual debug
        switch. Don't silently refuse; print the plan and warn so the
        user sees the state mismatch."""
        stopped = _scheduled_routine(rid="r1")
        stopped["state"] = "STOPPED"
        cfg = self._cfg_with_routines(tmp_path, [stopped])
        out = io.StringIO()
        err = io.StringIO()
        rc = orch.cli_main(
            ["test-fire", "--config", str(cfg), "--routine-id", "r1"],
            stdout=out,
            stderr=err,
        )
        assert rc == 0, "test-fire on STOPPED must still succeed (manual override)"
        assert "r1" in out.getvalue(), "plan must still mention the routine"
        # Warning goes to stderr so the plan on stdout stays pipeable.
        warn = err.getvalue().lower()
        assert "stopped" in warn or "warn" in warn or "not active" in warn, (
            "stderr must call out the STOPPED state so the user notices"
        )


# ---------------------------------------------------------------------------
# budget — re-apply the cadence preset table without re-running the interview
#
# goal.md (Token frugality): "Add a `/auto-routines budget <tier>` command
# that re-applies the cadence preset table to the live config + scheduled
# tasks. Lets the user dial up or down without re-running the full interview."
#
# Surface: `python scripts/orchestrator.py budget --config <path> --tier <tier>`
# Rewrites `meta.budget` + cron expressions for routines named in the
# preset table for that tier. Untouched routines stay as-is (no silent
# downgrade of routines the preset doesn't mention).
# ---------------------------------------------------------------------------

class TestBudget:
    def _cfg_with_routines(self, tmp_path, routines, meta_extra=None):
        import yaml
        cfg = _v4_config(routines)
        if meta_extra:
            cfg["meta"].update(meta_extra)
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg))
        return p

    def _read(self, path):
        import yaml
        return yaml.safe_load(path.read_text())

    def _prd_routine(self):
        # Mirrors the self-hosted prd-implement entry shape: scheduled
        # with a trigger { cron, human }.
        return {
            "id": "prd-implement",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
            "purpose": "drive PRD",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 5,
        }

    def _commit_tests_routine(self):
        # git-hook routine — no cron; budget must NOT touch this.
        return {
            "id": "commit-tests",
            "state": "ACTIVE",
            "primitive": "git-hook",
            "trigger": {"human": "on every git commit"},
            "purpose": "test on commit",
            "automation_level": "auto",
        }

    def test_budget_medium_writes_meta_budget_field(self, orch, tmp_path):
        cfg = self._cfg_with_routines(tmp_path, [self._prd_routine()])
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "medium"],
            stdout=io.StringIO(),
        )
        assert rc == 0
        new = self._read(cfg)
        assert new["meta"]["budget"] == "medium", (
            "budget command must update meta.budget so subsequent runs "
            "see the new tier"
        )

    def test_budget_medium_rewrites_prd_implement_cron(self, orch, tmp_path):
        """The preset table in SKILL.md says medium / prd-implement →
        every 12h (`0 */12 * * *`). Pin that the budget command actually
        applies it."""
        cfg = self._cfg_with_routines(tmp_path, [self._prd_routine()])
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "medium"],
            stdout=io.StringIO(),
        )
        assert rc == 0
        new = self._read(cfg)
        prd = next(r for r in new["routines"] if r["id"] == "prd-implement")
        assert prd["trigger"]["cron"] == "0 */12 * * *", (
            f"medium / prd-implement should be every 12h, "
            f"got {prd['trigger']['cron']!r}"
        )
        # The human label must move with the cron — silent drift between
        # the two values is exactly the bug this command is meant to fix.
        assert "12" in prd["trigger"]["human"], (
            f"human label must reflect the new cadence, "
            f"got {prd['trigger']['human']!r}"
        )

    def test_budget_high_rewrites_prd_implement_to_every_4h(self, orch, tmp_path):
        # Start in medium so the test demonstrates an *actual* change.
        prd = self._prd_routine()
        prd["trigger"]["cron"] = "0 */12 * * *"
        prd["trigger"]["human"] = "every 12 hours"
        cfg = self._cfg_with_routines(tmp_path, [prd])
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "high"],
            stdout=io.StringIO(),
        )
        assert rc == 0
        new = self._read(cfg)
        prd_new = next(r for r in new["routines"] if r["id"] == "prd-implement")
        assert prd_new["trigger"]["cron"] == "0 */4 * * *"
        assert "4" in prd_new["trigger"]["human"]

    def test_budget_preserves_unrelated_routines(self, orch, tmp_path):
        """git-hook routines have no cron and aren't in the preset table.
        The budget command must NOT touch them — silent rewriting of an
        unrelated routine is the worst possible failure mode for a
        config-mutation command."""
        cfg = self._cfg_with_routines(
            tmp_path,
            [self._prd_routine(), self._commit_tests_routine()],
        )
        before = self._read(cfg)
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "low"],
            stdout=io.StringIO(),
        )
        assert rc == 0
        after = self._read(cfg)
        before_ct = next(r for r in before["routines"] if r["id"] == "commit-tests")
        after_ct = next(r for r in after["routines"] if r["id"] == "commit-tests")
        assert before_ct == after_ct, (
            "commit-tests is not in the preset table — budget command "
            "must leave it byte-identical. Silent rewrites of unrelated "
            "routines is the bug class this test exists to prevent."
        )

    def test_budget_rejects_unknown_tier(self, orch, tmp_path):
        cfg = self._cfg_with_routines(tmp_path, [self._prd_routine()])
        before = cfg.read_text()
        err = io.StringIO()
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "extreme"],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc != 0, "unknown tier must fail"
        # Specific named error mentioning the bad value
        assert "extreme" in err.getvalue() or "tier" in err.getvalue().lower()
        # Config must be byte-identical — failed validation must not
        # leave a half-rewritten file on disk.
        assert cfg.read_text() == before, (
            "budget must not partially mutate config when tier is invalid"
        )


# ---------------------------------------------------------------------------
# Budget command: MCP update plan emission
# ---------------------------------------------------------------------------
# PRD goal.md (Token frugality): "Add a `/auto-routines budget <tier>`
# command that re-applies the cadence preset table to the live config
# + scheduled tasks."
#
# The CLI already handles the config half. For the scheduled-tasks half,
# the CLI emits an `mcp-plan:` block — one JSON line per touched
# routine — listing the task_id and new cron the LLM caller must pass
# to `mcp__scheduled-tasks__update_scheduled_task`. The CLI itself
# can't call MCPs (no MCP surface from a python subprocess); the plan
# turns the SKILL.md follow-up step from "scan config and figure out
# what to update" into "iterate provided lines."

class TestBudgetMcpPlan:
    def _cfg(self, tmp_path, routines, meta_extra=None):
        import yaml
        cfg = _v4_config(routines)
        if meta_extra:
            cfg["meta"].update(meta_extra)
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg))
        return p

    def _prd_with_task_id(self, task_id="tsk_prd_001"):
        return {
            "id": "prd-implement",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
            "purpose": "drive PRD",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 5,
            "task_id": task_id,
        }

    def _digest_with_task_id(self, task_id="tsk_digest_002"):
        return {
            "id": "daily-digest",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "0 19 * * *", "human": "7:00 PM daily"},
            "purpose": "digest",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 3,
            "task_id": task_id,
        }

    def test_plan_emits_one_line_per_touched_routine(self, orch, tmp_path):
        """For each rewritten routine that has a stored `task_id`, the
        CLI must emit a parseable plan line. Otherwise the SKILL.md
        follow-up step has to scan the config itself — which is what
        we're trying to remove."""
        cfg = self._cfg(
            tmp_path,
            [self._prd_with_task_id(), self._digest_with_task_id()],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "medium"],
            stdout=out,
        )
        assert rc == 0
        text = out.getvalue()
        assert "mcp-plan:" in text, (
            "budget output must include an `mcp-plan:` block so the "
            "SKILL.md Mode can iterate update_scheduled_task calls "
            "without re-scanning the config"
        )
        # Both touched routines must appear (medium tier rewrites both
        # prd-implement and daily-digest).
        assert "prd-implement" in text
        assert "daily-digest" in text

    def test_plan_carries_task_id_and_new_cron(self, orch, tmp_path):
        """Each plan line must carry the stored task_id AND the new
        cron — the LLM step calls `update_scheduled_task(task_id, cron)`
        verbatim from this line. If either field is missing the
        follow-up step is back to scanning."""
        cfg = self._cfg(
            tmp_path,
            [self._prd_with_task_id("tsk_prd_abc")],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "medium"],
            stdout=out,
        )
        assert rc == 0
        text = out.getvalue()
        # Find the plan block. Lines inside it must be JSON-decodable
        # so the LLM step doesn't have to do ad-hoc parsing.
        assert "tsk_prd_abc" in text, (
            "plan must include the stored task_id verbatim — without "
            "it, the update_scheduled_task call has no target"
        )
        assert "0 */12 * * *" in text, (
            "plan must include the new cron (medium / prd-implement = "
            "every 12 hours) so the LLM doesn't have to re-derive it"
        )

    def test_plan_lines_are_machine_parseable(self, orch, tmp_path):
        """The plan must be JSON per line — ad-hoc string formats are
        the bug class this test exists to prevent."""
        import json
        cfg = self._cfg(
            tmp_path,
            [self._prd_with_task_id(), self._digest_with_task_id()],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "high"],
            stdout=out,
        )
        assert rc == 0
        text = out.getvalue()
        # Extract lines between the `mcp-plan:` marker and either the
        # next blank line, the next `#`-prefixed header line, or EOF.
        lines = text.splitlines()
        plan_idx = next(i for i, ln in enumerate(lines) if "mcp-plan:" in ln)
        plan_lines = []
        for ln in lines[plan_idx + 1:]:
            s = ln.strip()
            if not s:
                break
            if s.startswith("#"):
                # Comment / warning lines inside the plan are allowed
                # for warnings (e.g. routine missing task_id); skip.
                continue
            plan_lines.append(s)
        assert plan_lines, "plan block must contain at least one entry"
        # Each non-comment line must be a JSON object with at least
        # `routine_id`, `task_id`, `cron` keys.
        for s in plan_lines:
            obj = json.loads(s)  # crashes if not JSON — that's the assertion
            assert "routine_id" in obj
            assert "task_id" in obj
            assert "cron" in obj

    def test_plan_warns_when_task_id_missing(self, orch, tmp_path):
        """A routine in the preset but with no stored `task_id` (e.g.
        a hand-edited config or a routine that hasn't been installed
        via the orchestrator) must surface a warning line — silently
        skipping it would leave a stale cron in the live MCP."""
        prd = self._prd_with_task_id()
        del prd["task_id"]  # simulate missing
        cfg = self._cfg(tmp_path, [prd])
        out = io.StringIO()
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "medium"],
            stdout=out,
        )
        assert rc == 0, (
            "missing task_id is a warning, not a hard failure — the "
            "config rewrite still succeeds"
        )
        text = out.getvalue().lower()
        assert "prd-implement" in text and (
            "warn" in text or "missing task_id" in text or "no task_id" in text
        ), (
            "budget output must warn when a touched routine has no "
            "stored task_id — otherwise the LLM step silently leaves "
            "the live MCP cron stale"
        )

    def test_plan_omits_routines_not_in_preset(self, orch, tmp_path):
        """commit-tests is a git-hook routine and never in the preset
        table. The plan must not list it — the MCP doesn't track
        git-hook routines, and including them would prompt a spurious
        update_scheduled_task call."""
        # Include a scheduled routine the preset doesn't touch (a
        # custom routine_id absent from BUDGET_PRESETS).
        custom = {
            "id": "custom-watcher",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "0 8 * * *", "human": "8 AM daily"},
            "purpose": "custom",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 2,
            "task_id": "tsk_custom_x",
        }
        cfg = self._cfg(tmp_path, [self._prd_with_task_id(), custom])
        out = io.StringIO()
        rc = orch.cli_main(
            ["budget", "--config", str(cfg), "--tier", "medium"],
            stdout=out,
        )
        assert rc == 0
        text = out.getvalue()
        # custom-watcher must NOT appear in the plan (it's not in the
        # preset, so its cron is untouched; emitting it would be a lie).
        # The substring check is sufficient — if the id appears anywhere
        # in the output, the plan is leaking it.
        plan_section = text.split("mcp-plan:", 1)[1]
        assert "custom-watcher" not in plan_section, (
            "plan must not list routines outside the preset — "
            "including them would prompt update_scheduled_task calls "
            "with cron values the budget command never set"
        )


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


# ---------------------------------------------------------------------------
# first-pr-eta — surface the first forward-driving routine's next fire
# ---------------------------------------------------------------------------
# PRD `.iteration/goal.md` (Skill UX): "Surface the first routine PR
# opened by a fresh install in the welcome output ('your first auto-PR
# will land at ~6:00 PM')." Step 8 of init invokes this subcommand to
# render the ETA line — pure-script, no LLM tokens. The routine's
# stored `trigger.human` is the source of truth (sanity-check pins it
# present whenever a cron is set), so we read it directly rather than
# reparse the cron.

class TestFirstPrEta:
    def _make_catalog(self, tmp_path, archetypes):
        """Write a minimal catalog with the given archetypes."""
        import yaml
        # Each entry: (id, category, primitive)
        cat = {
            "archetypes": [
                {
                    "id": rid,
                    "category": category,
                    "primitive": primitive,
                    "purpose": "x",
                    "trigger_default": "test",
                    "automation_default": "auto",
                    "self_evolve": False,
                    "success_criterion": "",
                    "stack_hints": [],
                    "prompt_body": "x",
                }
                for rid, category, primitive in archetypes
            ]
        }
        p = tmp_path / "catalog.yaml"
        p.write_text(yaml.safe_dump(cat))
        return p

    def _make_config(self, tmp_path, routines):
        """Write a v4 config with the given routines (id, primitive, human)."""
        import yaml
        cfg = _v4_config([
            {
                "id": rid,
                "state": "ACTIVE",
                "primitive": primitive,
                "trigger": {"cron": "0 9 * * *", "human": human},
                "purpose": "x",
                "automation_level": "auto",
                "execution_surface": "gha",
                "est_minutes": 5,
            }
            for rid, primitive, human in routines
        ])
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg))
        return p

    def test_prints_eta_for_forward_driving_routine(self, orch, tmp_path):
        """A config with one forward-driving routine must produce a
        welcome line naming that routine's trigger.human."""
        catalog = self._make_catalog(
            tmp_path, [("prd-implement", "forward-driving", "scheduled")]
        )
        cfg = self._make_config(
            tmp_path, [("prd-implement", "scheduled", "every 12 hours")]
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["first-pr-eta", "--config", str(cfg), "--catalog", str(catalog)],
            stdout=out,
        )
        assert rc == 0, "subcommand must succeed when a forward-driving routine exists"
        text = out.getvalue()
        # PRD's example phrasing: "your first auto-PR will land at ~6:00 PM".
        # Be loose on exact wording, strict on the schedule appearing.
        assert "every 12 hours" in text, (
            "output must include the routine's trigger.human so the "
            "user knows WHEN to expect the PR — got:\n" + text
        )
        # And surface that this is about PR ETA, not just "next fire".
        text_lower = text.lower()
        assert "pr" in text_lower, (
            "output must mention 'PR' so the user knows this is the "
            "ETA framing, not a generic schedule print"
        )

    def test_picks_first_forward_driving_routine_in_config_order(
        self, orch, tmp_path
    ):
        """Determinism: when multiple forward-driving routines exist,
        the subcommand picks the FIRST one in config order. Otherwise
        the welcome output flips between runs and confuses operators."""
        catalog = self._make_catalog(
            tmp_path,
            [
                ("prd-implement", "forward-driving", "scheduled"),
                ("weekly-dep-audit", "forward-driving", "scheduled"),
                ("daily-digest", "reactive", "scheduled"),
            ],
        )
        cfg = self._make_config(
            tmp_path,
            [
                ("prd-implement", "scheduled", "every 12 hours"),
                ("weekly-dep-audit", "scheduled", "Mondays 9 AM"),
                ("daily-digest", "scheduled", "6 PM daily"),
            ],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["first-pr-eta", "--config", str(cfg), "--catalog", str(catalog)],
            stdout=out,
        )
        assert rc == 0
        text = out.getvalue()
        assert "every 12 hours" in text, (
            "must pick prd-implement (first forward-driving in config "
            "order) not weekly-dep-audit"
        )
        assert "Mondays 9 AM" not in text, (
            "must not include the second forward-driving routine's "
            "trigger — output should name exactly one ETA"
        )

    def test_skips_reactive_routines(self, orch, tmp_path):
        """Reactive routines (commit-tests, daily-digest, …) don't open
        PRs on a schedule the user is waiting for. They must not be
        named in the welcome ETA."""
        catalog = self._make_catalog(
            tmp_path,
            [
                ("daily-digest", "reactive", "scheduled"),
                ("commit-tests", "reactive", "git-hook"),
            ],
        )
        cfg = self._make_config(
            tmp_path,
            [
                ("daily-digest", "scheduled", "6 PM daily"),
                ("commit-tests", "git-hook", "on every commit"),
            ],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["first-pr-eta", "--config", str(cfg), "--catalog", str(catalog)],
            stdout=out,
        )
        # No forward-driving routines installed — should print a stub
        # message and exit 0 (not an error: a reactive-only install is
        # a valid configuration).
        assert rc == 0
        text = out.getvalue()
        assert "6 PM daily" not in text, (
            "reactive routine's schedule must NOT appear in the ETA"
        )
        assert "on every commit" not in text
        # Stub phrasing: any reasonable "no forward-driving installed"
        # message.
        text_lower = text.lower()
        assert (
            "no forward-driving" in text_lower
            or "no forward driving" in text_lower
            or "react" in text_lower  # "reactive-only install"
            or "no scheduled pr" in text_lower
        ), (
            "must emit a stub when no forward-driving routine is "
            "installed — silent output looks like the script broke"
        )

    def test_ignores_routines_missing_from_catalog(self, orch, tmp_path):
        """A user config can reference routine ids that aren't in the
        catalog (custom routines, or catalog drift between versions).
        Those must be silently skipped, not crash the subcommand —
        the welcome output never blocks install completion."""
        catalog = self._make_catalog(
            tmp_path, [("prd-implement", "forward-driving", "scheduled")]
        )
        cfg = self._make_config(
            tmp_path,
            [
                ("custom-not-in-catalog", "scheduled", "every 5m"),
                ("prd-implement", "scheduled", "every 4 hours"),
            ],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["first-pr-eta", "--config", str(cfg), "--catalog", str(catalog)],
            stdout=out,
        )
        assert rc == 0
        text = out.getvalue()
        # Should pick prd-implement (the only one in catalog with
        # forward-driving category) and ignore custom-not-in-catalog.
        assert "every 4 hours" in text
        assert "every 5m" not in text

    def test_outputs_single_line(self, orch, tmp_path):
        """The welcome ETA must be one line so step 8's printf-style
        output stays compact. Multi-line output would push the welcome
        block past one screen."""
        catalog = self._make_catalog(
            tmp_path, [("prd-implement", "forward-driving", "scheduled")]
        )
        cfg = self._make_config(
            tmp_path, [("prd-implement", "scheduled", "every 12 hours")]
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["first-pr-eta", "--config", str(cfg), "--catalog", str(catalog)],
            stdout=out,
        )
        assert rc == 0
        # Allow one trailing newline; anything else is multi-line.
        text = out.getvalue().rstrip("\n")
        assert "\n" not in text, (
            f"ETA output must be a single line; got:\n{text!r}"
        )


# ---------------------------------------------------------------------------
# Drain evolve-requests
# ---------------------------------------------------------------------------
# PRD goal.md (Coverage and correctness): "Add tests for the `evolve`
# flow — drain `evolve_requests.jsonl`, perform the FSM transitions,
# write a checkpoint, apply, verify."
#
# The evolve flow as a whole lives in SKILL.md `Mode: evolve` (LLM-
# driven). But the *drain step* is pure mechanics — read JSONL,
# validate each line, emit a parseable plan of which routines move
# ACTIVE → EVOLVING. Pulling it into a pure-script subcommand lets
# us pin the contract in code (and saves the LLM from re-parsing
# the file on every evolve fire).
#
# The drain subcommand:
#   - reads `.iteration/evolve_requests.jsonl`
#   - validates each line against the schema (ts, routine_id, reason,
#     suggested — same shape SKILL.md "Mid-run self-evolution" pins)
#   - emits one JSON line per valid request to stdout (the plan)
#   - emits `# warn:` lines for malformed entries (LLM surfaces them
#     but doesn't have to retry the parse itself)
#   - default is dry-run (file untouched); `--apply` truncates after
#     a successful drain — atomic via tempfile + os.replace so a
#     crash mid-drain doesn't lose un-processed entries

class TestDrainEvolveRequests:
    def _write_jsonl(self, tmp_path, entries):
        """Write entries (list of dicts) one-per-line to a tmp jsonl."""
        p = tmp_path / "evolve_requests.jsonl"
        with open(p, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return p

    def _valid_request(self, **overrides):
        base = {
            "ts": "2026-05-10T14:32:00-0700",
            "routine_id": "pr-ci-watcher",
            "reason": "CI flake rate at 0% over last 200 PRs",
            "suggested": "reduce frequency",
        }
        base.update(overrides)
        return base

    def _read_plan_lines(self, text):
        """Extract JSON-decodable plan lines from drain output."""
        out_lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            out_lines.append(json.loads(s))
        return out_lines

    def test_drain_emits_plan_line_per_valid_request(self, orch, tmp_path):
        """The whole reason this subcommand exists: turn a jsonl file
        into a stream of validated, parseable plan lines so the LLM
        step iterates instead of parses."""
        f = self._write_jsonl(
            tmp_path,
            [
                self._valid_request(routine_id="pr-ci-watcher"),
                self._valid_request(routine_id="daily-digest"),
            ],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
        )
        assert rc == 0, out.getvalue()
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 2
        ids = {entry["routine_id"] for entry in plan}
        assert ids == {"pr-ci-watcher", "daily-digest"}

    def test_drain_dry_run_does_not_truncate(self, orch, tmp_path):
        """Default mode reads but doesn't mutate — so re-running it
        produces the same output. The LLM may invoke this multiple
        times in a single evolve fire (e.g. to re-display the plan
        after a sanity-check)."""
        f = self._write_jsonl(tmp_path, [self._valid_request()])
        before = f.read_text()
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
        )
        assert rc == 0
        assert f.read_text() == before, (
            "default drain (no --apply) must NOT mutate the jsonl file"
        )

    def test_drain_apply_truncates_after_emit(self, orch, tmp_path):
        """`--apply` is the commit half. After a successful drain the
        file is empty. SKILL.md step 2 pins this — once a request
        has been processed it must not fire again on the next evolve."""
        f = self._write_jsonl(tmp_path, [self._valid_request()])
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f), "--apply"],
            stdout=out,
        )
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1
        assert f.read_text() == "", (
            "--apply must truncate the jsonl file so a re-drain "
            "produces no plan lines"
        )

    def test_drain_missing_file_is_a_noop(self, orch, tmp_path):
        """A fresh repo has no `evolve_requests.jsonl` yet. Drain must
        succeed with zero plan lines — not error — so the SKILL.md
        step doesn't need a `[ -f ... ]` guard."""
        f = tmp_path / "does-not-exist.jsonl"
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
        )
        assert rc == 0
        assert self._read_plan_lines(out.getvalue()) == []

    def test_drain_empty_file_emits_zero_plan_lines(self, orch, tmp_path):
        f = self._write_jsonl(tmp_path, [])
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
        )
        assert rc == 0
        assert self._read_plan_lines(out.getvalue()) == []

    def test_drain_warns_on_malformed_json(self, orch, tmp_path):
        """A garbled line must NOT abort the whole drain — valid lines
        after it must still emit. The malformed line surfaces as a
        `# warn:` so the LLM step can show the user what was rejected."""
        f = tmp_path / "evolve_requests.jsonl"
        f.write_text(
            "this is not json\n"
            + json.dumps(self._valid_request(routine_id="daily-digest")) + "\n"
        )
        out = io.StringIO()
        err = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
            stderr=err,
        )
        assert rc == 0, "malformed lines are warnings, not hard fails"
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1 and plan[0]["routine_id"] == "daily-digest", (
            "valid lines after a malformed one must still emit"
        )
        # The warning surfaces somewhere visible — either stdout
        # (alongside the plan, as a # warn: line) or stderr.
        combined = (out.getvalue() + err.getvalue()).lower()
        assert "warn" in combined or "invalid" in combined or "malformed" in combined, (
            "drain must surface a warning for the malformed line so "
            "the user knows their request was rejected"
        )

    def test_drain_warns_on_missing_required_field(self, orch, tmp_path):
        """A request missing `routine_id` is malformed — the FSM
        transition can't target an anonymous routine. Treat it the
        same as malformed JSON."""
        f = self._write_jsonl(
            tmp_path,
            [
                {"ts": "2026-05-10T14:32:00-0700", "reason": "x", "suggested": "y"},
                self._valid_request(routine_id="daily-digest"),
            ],
        )
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
        )
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1
        assert plan[0]["routine_id"] == "daily-digest"

    def test_drain_apply_does_not_truncate_on_warning_only_input(self, orch, tmp_path):
        """Edge case: if every line is malformed, --apply should NOT
        truncate — the user might want to fix-and-retry. (Truncating
        would silently swallow whatever they meant to send.)"""
        f = tmp_path / "evolve_requests.jsonl"
        f.write_text("not json\nalso not json\n")
        before = f.read_text()
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f), "--apply"],
            stdout=out,
        )
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert plan == []
        assert f.read_text() == before, (
            "--apply must NOT truncate when zero valid plan lines were "
            "produced — silently dropping all the user's requests is the "
            "worst possible failure mode"
        )

    def test_drain_plan_lines_carry_full_request_shape(self, orch, tmp_path):
        """Each plan line must carry every field the LLM step expects:
        ts, routine_id, reason, suggested. Otherwise the SKILL.md step
        has to re-read the source file — defeating the point of the
        drain."""
        req = self._valid_request(
            routine_id="pr-ci-watcher",
            reason="testing reason",
            suggested="stop",
        )
        f = self._write_jsonl(tmp_path, [req])
        out = io.StringIO()
        rc = orch.cli_main(
            ["drain-evolve-requests", "--file", str(f)],
            stdout=out,
        )
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        entry = plan[0]
        for k in ("ts", "routine_id", "reason", "suggested"):
            assert k in entry, f"plan line missing field {k!r}"
        assert entry["routine_id"] == "pr-ci-watcher"
        assert entry["suggested"] == "stop"
        assert entry["reason"] == "testing reason"


# ---------------------------------------------------------------------------
# fsm-plan: deterministic ACTIVE→STAGNANT transition detector
# ---------------------------------------------------------------------------
# PRD `.iteration/goal.md` (Coverage and correctness): "Add tests for the
# `evolve` flow — drain evolve_requests.jsonl, perform the FSM
# transitions, write a checkpoint, apply, verify." The drain half
# shipped in TestDrainEvolveRequests. This slice ships the FSM-transition
# half — but ONLY the deterministic transitions (ACTIVE→STAGNANT). The
# other transitions (STAGNANT→ACTIVE reactivation, ACTIVE→COMPLETED on
# success_criterion-met) require natural-language signal interpretation
# and stay in LLM territory.
#
# Stagnation is pure arithmetic: runs since last_useful_iter >=
# stagnation_threshold → STAGNANT. Extracting this to a pure-script
# subcommand means the SKILL.md Mode: evolve step doesn't have to
# eyeball stats and threshold math — it just iterates the plan lines.

class TestFsmPlan:
    """Pin the contract of `orchestrator.py fsm-plan`.

    Output shape mirrors `drain-evolve-requests`: warnings on stdout
    as `# warn:` lines, one JSON object per transition. `--apply` is
    intentionally NOT added in this slice — applying the transition
    means rewriting config.yaml's `routines[].state`, which is the
    next atomic concern. This subcommand is read-only."""

    def _write_config(self, tmp_path, routines, meta=None):
        """Write a minimal v4 config and return the path. Uses the
        full _v4_config shape so the orchestrator's loader doesn't
        choke on missing top-level keys it later cares about."""
        import yaml
        cfg = _v4_config(routines)
        if meta is not None:
            cfg["meta"].update(meta)
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg))
        return p

    def _routine(self, **overrides):
        """A canonical ACTIVE routine with stats. Override any field.
        Default values picked so the routine is JUST short of stagnant
        (runs=5, threshold=7) — tests bump runs above threshold to
        trigger a transition. Cheaper than rebuilding from scratch
        in every test."""
        base = {
            "id": "pr-ci-watcher",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "*/30 * * * *", "human": "every 30 min"},
            "purpose": "test",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 5,
            "stagnation_threshold": 7,
            "stats": {
                "runs": 5,
                "useful": 0,
                "noisy": 5,
                "last_useful_iter": None,
            },
        }
        # Stats overrides merge into stats dict, not replace it.
        if "stats" in overrides:
            base["stats"] = {**base["stats"], **overrides.pop("stats")}
        base.update(overrides)
        return base

    def _read_plan_lines(self, text):
        out_lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            out_lines.append(json.loads(s))
        return out_lines

    # ---- core arithmetic --------------------------------------------------

    def test_runs_above_threshold_emits_stagnant_transition(self, orch, tmp_path):
        """The canonical case: an ACTIVE routine has run more than
        `stagnation_threshold` times without producing a useful
        outcome (last_useful_iter is null) → transition to STAGNANT.

        This is the WHOLE reason this subcommand exists. If this
        test passes, the SKILL.md Mode: evolve step can iterate
        plan lines instead of doing stats arithmetic in prose."""
        cfg = self._write_config(
            tmp_path,
            [self._routine(stats={"runs": 10, "last_useful_iter": None})],
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0, out.getvalue()
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1
        entry = plan[0]
        assert entry["routine_id"] == "pr-ci-watcher"
        assert entry["from"] == "ACTIVE"
        assert entry["to"] == "STAGNANT"

    def test_runs_below_threshold_emits_no_transition(self, orch, tmp_path):
        """The complementary case. Routine has run, but not enough
        times to be stagnant yet. A transition here would be a
        false-positive that paused a working routine — pin the
        non-trigger condition."""
        cfg = self._write_config(
            tmp_path,
            [self._routine(stats={"runs": 3, "last_useful_iter": None})],
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        assert self._read_plan_lines(out.getvalue()) == []

    def test_runs_equal_to_threshold_emits_transition(self, orch, tmp_path):
        """Boundary: runs == threshold. The schema says 'consecutive
        runs with stats.useful flat → STAGNANT' — once we've hit the
        threshold-th unuseful run, that IS stagnation. Pinning the
        boundary so a future off-by-one doesn't silently flip
        behavior. (Inclusive boundary: `>=`, not `>`.)"""
        cfg = self._write_config(
            tmp_path,
            [self._routine(
                stagnation_threshold=7,
                stats={"runs": 7, "last_useful_iter": None},
            )],
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1, (
            "runs == threshold must trigger STAGNANT (inclusive boundary). "
            "If this fails, the comparison is `>` not `>=` — the routine "
            "would have to run threshold+1 times before pausing, which "
            "drifts past the configured patience."
        )

    def test_last_useful_iter_subtracted_when_set(self, orch, tmp_path):
        """If `last_useful_iter` is set, only count runs SINCE that
        iteration toward stagnation. Otherwise a long-running routine
        that produced useful work last week would be marked stagnant
        on its 8th run total — a false positive."""
        cfg = self._write_config(
            tmp_path,
            [self._routine(
                stagnation_threshold=7,
                stats={"runs": 100, "useful": 50, "last_useful_iter": 95},
            )],
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        # 100 - 95 = 5 runs since last useful; threshold=7; NOT stagnant.
        assert self._read_plan_lines(out.getvalue()) == [], (
            "runs (100) - last_useful_iter (95) = 5 < threshold (7); "
            "must NOT mark stagnant — the routine produced useful work "
            "recently. False positive here would pause an active "
            "contributor."
        )

    def test_last_useful_iter_subtracted_past_threshold_emits_transition(self, orch, tmp_path):
        """Counterpart: same shape, but the gap since last_useful_iter
        crosses the threshold. Must emit a STAGNANT plan line."""
        cfg = self._write_config(
            tmp_path,
            [self._routine(
                stagnation_threshold=7,
                stats={"runs": 100, "useful": 1, "last_useful_iter": 50},
            )],
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1
        assert plan[0]["to"] == "STAGNANT"

    # ---- threshold resolution --------------------------------------------

    def test_per_routine_threshold_overrides_meta_default(self, orch, tmp_path):
        """Per-routine `stagnation_threshold` always wins over
        `meta.default_stagnation_threshold`. Without this precedence
        a user-configured short-patience routine would be ignored in
        favor of the cluster-wide default."""
        cfg = self._write_config(
            tmp_path,
            [self._routine(
                stagnation_threshold=3,  # short-patience override
                stats={"runs": 4, "last_useful_iter": None},
            )],
            meta={"default_stagnation_threshold": 100},  # very patient cluster default
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1, (
            "per-routine threshold (3) must win over meta default (100); "
            "got no transition — the meta default is leaking through"
        )

    def test_meta_default_used_when_per_routine_unset(self, orch, tmp_path):
        """When a routine omits `stagnation_threshold`, the meta
        default applies. A fresh interview-driven install only sets
        the meta default; pin that fallback path."""
        r = self._routine(stats={"runs": 4, "last_useful_iter": None})
        r.pop("stagnation_threshold")  # rely on meta default
        cfg = self._write_config(
            tmp_path,
            [r],
            meta={"default_stagnation_threshold": 3},
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1, (
            "no per-routine threshold → must fall back to "
            "meta.default_stagnation_threshold (3); 4 >= 3 should fire."
        )

    # ---- state filtering --------------------------------------------------

    def test_non_active_routines_are_skipped(self, orch, tmp_path):
        """Only ACTIVE routines are candidates for ACTIVE→STAGNANT.
        Every other state is either already-paused (STAGNANT,
        COMPLETED, STOPPED), transient/owned by a different transition
        (EVOLVING), or pre-confirm (PROPOSED). Re-marking any of them
        as stagnant would be either no-op (STAGNANT) or wrong."""
        states_that_should_be_skipped = [
            "PROPOSED", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED",
        ]
        routines = []
        for i, st in enumerate(states_that_should_be_skipped):
            r = self._routine(
                id=f"skip-{i}",
                state=st,
                stagnation_threshold=1,  # would trigger if state weren't filtered
                stats={"runs": 99, "last_useful_iter": None},
            )
            # Manual id override since _routine doesn't accept it via kwarg
            # patten (override path) — set explicitly after.
            r["id"] = f"skip-{i}"
            routines.append(r)
        cfg = self._write_config(tmp_path, routines)
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        assert self._read_plan_lines(out.getvalue()) == [], (
            "non-ACTIVE routines must be skipped regardless of stats; "
            f"got plan lines for: "
            f"{[p['routine_id'] for p in self._read_plan_lines(out.getvalue())]}"
        )

    # ---- output shape -----------------------------------------------------

    def test_plan_line_carries_routine_id_from_to_and_reason(self, orch, tmp_path):
        """Each plan line must be self-contained — routine_id, from
        state, to state, and a human-readable reason. The reason
        anchors the LLM step's eventual user-facing message ('we
        paused pr-ci-watcher because...'). If reason is missing the
        SKILL.md step has to re-derive it, defeating the point of
        the deterministic emit."""
        cfg = self._write_config(
            tmp_path,
            [self._routine(
                stagnation_threshold=7,
                stats={"runs": 12, "last_useful_iter": None},
            )],
        )
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        plan = self._read_plan_lines(out.getvalue())
        assert len(plan) == 1
        entry = plan[0]
        for k in ("routine_id", "from", "to", "reason"):
            assert k in entry, f"plan line missing required field {k!r}"
        assert isinstance(entry["reason"], str) and entry["reason"], (
            "reason must be a non-empty string"
        )

    def test_empty_routines_emits_empty_plan(self, orch, tmp_path):
        """A fresh install with zero routines must emit no plan lines
        and exit 0. The SKILL.md step calls this unconditionally; a
        non-zero exit here would surface as a spurious error."""
        cfg = self._write_config(tmp_path, [])
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0
        assert self._read_plan_lines(out.getvalue()) == []

    # ---- robustness -------------------------------------------------------

    def test_missing_stats_is_treated_as_zero_runs(self, orch, tmp_path):
        """A freshly-installed routine has `stats: { runs: 0, ... }`,
        but a hand-edited config may omit `stats` entirely. Treat
        missing-or-empty stats as zero-runs — a brand new routine
        can't be stagnant. The robust handling matters: an early
        crash here would block every other routine's transition
        check in the same fire."""
        r = self._routine()
        r.pop("stats")  # config without stats block at all
        cfg = self._write_config(tmp_path, [r])
        out = io.StringIO()
        rc = orch.cli_main(["fsm-plan", "--config", str(cfg)], stdout=out)
        assert rc == 0, (
            f"missing stats must not crash the plan; got rc={rc}, "
            f"out={out.getvalue()!r}"
        )
        assert self._read_plan_lines(out.getvalue()) == [], (
            "routine with no stats yet → can't be stagnant"
        )

    def test_missing_config_returns_nonzero(self, orch, tmp_path):
        """A missing config path is a user error, not a no-op. Unlike
        drain-evolve-requests (where missing file = no requests yet),
        fsm-plan without a config has no work it CAN do — surfacing
        rc=1 lets the SKILL.md step abort cleanly."""
        missing = tmp_path / "does-not-exist.yaml"
        out = io.StringIO()
        err = io.StringIO()
        rc = orch.cli_main(
            ["fsm-plan", "--config", str(missing)],
            stdout=out,
            stderr=err,
        )
        assert rc != 0, "missing config must surface as non-zero exit"
