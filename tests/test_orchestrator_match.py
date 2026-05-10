"""
Tests for orchestrator.match_trigger() (PRD #10 Module 1, phase 3).

Contract:
    match_trigger(trigger, routines) -> list[routine]

Converts a raw trigger event from the dispatch surface (cron firing, hook
event, manual /run, etc.) into the `candidates` list that tick() consumes.
Pure: filters and returns a new list, no I/O, no mutation.

Splitting trigger-matching from dispatch-deciding (tick) keeps each
function small. The GHA workflow and the local hook bridge both call
`match_trigger` to figure out 'who's in scope' before handing off to tick.
"""
from __future__ import annotations

import copy
import importlib.util
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
# Builders
# ---------------------------------------------------------------------------

def make_routine(
    rid: str,
    *,
    primitive: str,
    cron: str | None = None,
    event: str | None = None,
    path_filters: list[str] | None = None,
) -> dict:
    trigger: dict = {}
    if cron is not None:
        trigger["cron"] = cron
        trigger["human"] = f"every cron {cron}"
    if event is not None:
        trigger["event"] = event
    routine = {
        "id": rid,
        "state": "ACTIVE",
        "primitive": primitive,
        "trigger": trigger,
        "purpose": "test",
        "automation_level": "auto",
    }
    if path_filters is not None:
        routine["path_filters"] = path_filters
    return routine


# ---------------------------------------------------------------------------
# Cron triggers
# ---------------------------------------------------------------------------

class TestCronTrigger:
    def test_matches_scheduled_routines_with_same_cron(self, orch):
        routines = [
            make_routine("a", primitive="scheduled", cron="*/30 * * * *"),
            make_routine("b", primitive="scheduled", cron="*/30 * * * *"),
            make_routine("c", primitive="scheduled", cron="0 9 * * *"),
        ]
        trigger = {"type": "cron", "cron_expr": "*/30 * * * *"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["a", "b"]

    def test_matches_pr_poll_routines_with_same_cron(self, orch):
        routines = [
            make_routine("a", primitive="pr-poll", cron="0 * * * *"),
            make_routine("b", primitive="pr-poll", cron="0 9 * * *"),
        ]
        trigger = {"type": "cron", "cron_expr": "0 * * * *"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["a"]

    def test_skips_non_scheduled_primitives(self, orch):
        """Cron triggers must NOT pick up hook/git-hook/loop routines, even
        if their trigger dict happens to have a 'cron' key."""
        routines = [
            make_routine("hook-r", primitive="hook", event="Stop"),
            make_routine("git-r", primitive="git-hook"),
            make_routine("loop-r", primitive="loop"),
            make_routine("sched-r", primitive="scheduled", cron="*/30 * * * *"),
        ]
        trigger = {"type": "cron", "cron_expr": "*/30 * * * *"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["sched-r"]

    def test_no_match_returns_empty_list(self, orch):
        routines = [
            make_routine("a", primitive="scheduled", cron="0 9 * * *"),
        ]
        trigger = {"type": "cron", "cron_expr": "*/30 * * * *"}
        assert orch.match_trigger(trigger, routines) == []

    def test_cron_match_is_string_exact(self, orch):
        """Equivalent crons (`*/30 * * * *` vs `0,30 * * * *`) are NOT
        considered equal by this layer — the trigger system has already
        decided which expression fired, so we string-match. Keeping
        equivalence checks out of here means no surprise double-fires."""
        routines = [
            make_routine("a", primitive="scheduled", cron="*/30 * * * *"),
            make_routine("b", primitive="scheduled", cron="0,30 * * * *"),
        ]
        trigger = {"type": "cron", "cron_expr": "*/30 * * * *"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["a"]


# ---------------------------------------------------------------------------
# Hook triggers (Claude Code session events)
# ---------------------------------------------------------------------------

class TestHookTrigger:
    def test_matches_hook_routines_by_event(self, orch):
        routines = [
            make_routine("a", primitive="hook", event="Stop"),
            make_routine("b", primitive="hook", event="SessionStart"),
            make_routine("c", primitive="hook", event="Stop"),
        ]
        trigger = {"type": "hook", "hook_event": "Stop"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["a", "c"]

    def test_skips_non_hook_primitives(self, orch):
        routines = [
            make_routine("sched", primitive="scheduled", cron="* * * * *"),
            make_routine("hook-r", primitive="hook", event="Stop"),
        ]
        trigger = {"type": "hook", "hook_event": "Stop"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["hook-r"]

    def test_no_event_match_returns_empty(self, orch):
        routines = [make_routine("a", primitive="hook", event="Stop")]
        trigger = {"type": "hook", "hook_event": "SessionStart"}
        assert orch.match_trigger(trigger, routines) == []


# ---------------------------------------------------------------------------
# git-hook triggers (post-commit shell hook)
# ---------------------------------------------------------------------------

class TestGitHookTrigger:
    def test_matches_all_git_hook_routines(self, orch):
        """git-hook has no event subtype — every git-hook routine fires
        on every post-commit. State + automation_level filter from there."""
        routines = [
            make_routine("a", primitive="git-hook"),
            make_routine("b", primitive="git-hook"),
            make_routine("c", primitive="hook", event="Stop"),
        ]
        trigger = {"type": "git-hook"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["a", "b"]


class TestGitHookPathFilterPriority:
    """PRD #10 priority rule 4: when the trigger reports `changed_files`
    and at least one git-hook routine declares a `path_filters` glob that
    matches one of those files, the matcher returns ONLY the matching
    routines.

    The canonical case is `goal.md` changing → `meta-evolve` fires alone,
    so cron-driven catch-up routines don't steal the slot.
    """

    def test_path_filter_match_short_circuits_to_matching_routine(self, orch):
        """When a routine's path_filters glob matches a changed file, that
        routine is selected ALONE — other git-hook routines are dropped.
        This is what implements the priority rule."""
        routines = [
            make_routine("commit-tests", primitive="git-hook"),
            make_routine("commit-lint", primitive="git-hook"),
            make_routine(
                "meta-evolve",
                primitive="git-hook",
                path_filters=[".iteration/goal.md"],
            ),
        ]
        trigger = {
            "type": "git-hook",
            "changed_files": [".iteration/goal.md", "scripts/orchestrator.py"],
        }
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["meta-evolve"]

    def test_no_path_filter_match_falls_back_to_all_git_hooks(self, orch):
        """If `changed_files` is supplied but no routine's path_filters
        match any of them, behave like the legacy git-hook trigger:
        return every git-hook routine. Catch-up commit-tests/commit-lint
        still need to run on plain code commits."""
        routines = [
            make_routine("commit-tests", primitive="git-hook"),
            make_routine("commit-lint", primitive="git-hook"),
            make_routine(
                "meta-evolve",
                primitive="git-hook",
                path_filters=[".iteration/goal.md"],
            ),
        ]
        trigger = {
            "type": "git-hook",
            "changed_files": ["scripts/orchestrator.py", "README.md"],
        }
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["commit-tests", "commit-lint", "meta-evolve"]

    def test_missing_changed_files_preserves_legacy_behavior(self, orch):
        """The trigger system may not always know which files changed
        (e.g. manual ticks). Without `changed_files` in the trigger dict,
        path_filters are ignored and every git-hook routine matches."""
        routines = [
            make_routine("commit-tests", primitive="git-hook"),
            make_routine(
                "meta-evolve",
                primitive="git-hook",
                path_filters=[".iteration/goal.md"],
            ),
        ]
        trigger = {"type": "git-hook"}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["commit-tests", "meta-evolve"]

    def test_glob_pattern_matches_in_subdir(self, orch):
        """path_filters supports fnmatch globs so a routine can subscribe
        to e.g. `docs/**/*.md` without listing every file. We use
        fnmatch.fnmatch semantics — `*` does NOT span path separators
        unless the pattern uses `**`."""
        routines = [
            make_routine("commit-tests", primitive="git-hook"),
            make_routine(
                "session-doc-drift",
                primitive="git-hook",
                path_filters=["docs/*.md"],
            ),
        ]
        trigger = {
            "type": "git-hook",
            "changed_files": ["docs/architecture.md"],
        }
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["session-doc-drift"]

    def test_multiple_routines_can_match_same_path(self, orch):
        """Two routines both subscribed to `.iteration/goal.md` both
        return — the priority short-circuit drops only routines without
        a matching filter, not other matching ones. tick() then
        FCFS-allocates them against the GHA-minute cap."""
        routines = [
            make_routine(
                "meta-evolve",
                primitive="git-hook",
                path_filters=[".iteration/goal.md"],
            ),
            make_routine(
                "goal-notify",
                primitive="git-hook",
                path_filters=[".iteration/goal.md"],
            ),
            make_routine("commit-tests", primitive="git-hook"),
        ]
        trigger = {
            "type": "git-hook",
            "changed_files": [".iteration/goal.md"],
        }
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["meta-evolve", "goal-notify"]

    def test_empty_changed_files_treated_as_unknown(self, orch):
        """An empty list is ambiguous: did nothing change, or did we
        just fail to compute the diff? Treat as unknown (== legacy
        behavior, return all git-hook routines) so a buggy caller can't
        starve catch-up routines forever."""
        routines = [
            make_routine("commit-tests", primitive="git-hook"),
            make_routine(
                "meta-evolve",
                primitive="git-hook",
                path_filters=[".iteration/goal.md"],
            ),
        ]
        trigger = {"type": "git-hook", "changed_files": []}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["commit-tests", "meta-evolve"]


# ---------------------------------------------------------------------------
# Manual triggers (user-invoked /run)
# ---------------------------------------------------------------------------

class TestManualTrigger:
    def test_matches_explicit_routine_ids(self, orch):
        routines = [
            make_routine("a", primitive="scheduled", cron="* * * * *"),
            make_routine("b", primitive="hook", event="Stop"),
            make_routine("c", primitive="git-hook"),
        ]
        trigger = {"type": "manual", "routine_ids": ["b", "c"]}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["b", "c"]

    def test_unknown_id_silently_ignored(self, orch):
        """Caller (e.g. /run) is responsible for reporting unknown ids
        to the user. The matcher just filters; never raises."""
        routines = [make_routine("a", primitive="scheduled", cron="* * * * *")]
        trigger = {"type": "manual", "routine_ids": ["nonexistent"]}
        assert orch.match_trigger(trigger, routines) == []

    def test_empty_routine_ids_returns_empty(self, orch):
        routines = [make_routine("a", primitive="scheduled", cron="* * * * *")]
        trigger = {"type": "manual", "routine_ids": []}
        assert orch.match_trigger(trigger, routines) == []

    def test_manual_ignores_primitive(self, orch):
        """If the user explicitly says 'run X', we don't second-guess.
        State and automation_level still apply downstream in tick()."""
        routines = [make_routine("a", primitive="loop")]
        trigger = {"type": "manual", "routine_ids": ["a"]}
        out = orch.match_trigger(trigger, routines)
        assert [r["id"] for r in out] == ["a"]


# ---------------------------------------------------------------------------
# Refusal / shape errors
# ---------------------------------------------------------------------------

class TestErrors:
    def test_unknown_trigger_type_raises(self, orch):
        with pytest.raises(ValueError, match="trigger.type"):
            orch.match_trigger({"type": "lunar-eclipse"}, [])

    def test_missing_trigger_type_raises(self, orch):
        with pytest.raises(ValueError, match="trigger.type"):
            orch.match_trigger({}, [])

    def test_cron_trigger_requires_cron_expr(self, orch):
        with pytest.raises(ValueError, match="cron_expr"):
            orch.match_trigger({"type": "cron"}, [])

    def test_hook_trigger_requires_event(self, orch):
        with pytest.raises(ValueError, match="hook_event"):
            orch.match_trigger({"type": "hook"}, [])

    def test_manual_trigger_requires_routine_ids(self, orch):
        with pytest.raises(ValueError, match="routine_ids"):
            orch.match_trigger({"type": "manual"}, [])


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------

class TestPurity:
    def test_does_not_mutate_routines(self, orch):
        routines = [
            make_routine("a", primitive="scheduled", cron="*/30 * * * *"),
            make_routine("b", primitive="hook", event="Stop"),
        ]
        snapshot = copy.deepcopy(routines)
        orch.match_trigger(
            {"type": "cron", "cron_expr": "*/30 * * * *"}, routines
        )
        assert routines == snapshot

    def test_does_not_mutate_trigger(self, orch):
        trigger = {"type": "cron", "cron_expr": "*/30 * * * *"}
        snapshot = copy.deepcopy(trigger)
        orch.match_trigger(trigger, [])
        assert trigger == snapshot

    def test_returns_new_list(self, orch):
        routines = [
            make_routine("a", primitive="scheduled", cron="*/30 * * * *"),
        ]
        out = orch.match_trigger(
            {"type": "cron", "cron_expr": "*/30 * * * *"}, routines
        )
        # Verify it's a new list — appending to it doesn't change input
        out.append({"id": "z"})
        assert len(routines) == 1


# ---------------------------------------------------------------------------
# End-to-end: match → tick
# ---------------------------------------------------------------------------

def test_match_then_tick_flow(orch):
    """Smoke test: the two functions compose — match_trigger's output is
    a valid `candidates` list for tick()."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    routines = [
        {
            "id": "fast",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "*/30 * * * *", "human": "every 30 minutes"},
            "purpose": "x",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 5,
        },
        {
            "id": "slow",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "0 9 * * *", "human": "9 AM"},
            "purpose": "x",
            "automation_level": "auto",
            "execution_surface": "local",
            "est_minutes": 5,
        },
    ]
    config = {
        "schema_version": 4,
        "repo_slug": "demo",
        "goal": "test",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": []},
        "routines": routines,
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
    state = {
        "schema_version": 1,
        "gha_minutes_used_today": 0,
        "gha_minutes_reset_date": "2026-05-10",
        "last_event_id": 0,
        "kill_switch_active": False,
        "last_dispatch": {},
    }
    trigger = {"type": "cron", "cron_expr": "*/30 * * * *"}
    candidates = orch.match_trigger(trigger, routines)
    assert [r["id"] for r in candidates] == ["fast"]
    now = dt.datetime(2026, 5, 10, 14, 0, tzinfo=ZoneInfo("UTC"))
    out = orch.tick(now, candidates, state, config)
    assert out["decisions"][0]["routine_id"] == "fast"
    assert out["decisions"][0]["action"] == "fire"
