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
