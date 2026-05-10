"""
Tests for orchestrator.tick() — the dispatch decision (PRD #10 Module 1, phase 2).

Contract:
    tick(now, candidates, state, config) -> {decisions, new_state}

`candidates` is the list of routines the trigger layer has already
filtered to. tick() decides WHICH to dispatch and on WHICH surface,
and produces an updated state.json. It does NOT match cron expressions
or hook events — that's a separate phase.

Each decision is:
    {routine_id, action: "fire"|"skip", surface: "gha"|"local"|None, reason}

new_state is a fresh dict (tick is pure — never mutates inputs).
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

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
    rid: str = "pr-watcher",
    *,
    state: str = "ACTIVE",
    primitive: str = "scheduled",
    surface: str | None = "local",
    est_minutes: int = 5,
    automation_level: str = "auto",
) -> dict:
    r = {
        "id": rid,
        "state": state,
        "primitive": primitive,
        "trigger": {"cron": "*/30 * * * *", "human": "every 30 minutes"},
        "purpose": "test",
        "automation_level": automation_level,
        "est_minutes": est_minutes,
    }
    if surface is not None:
        r["execution_surface"] = surface
    return r


def make_config(
    *,
    routines: list[dict],
    idle_window: str = "always",
    idle_window_tz: str | None = None,
    gha_minutes_cap: int = 60,
    kill_switch: bool = False,
) -> dict:
    meta = {
        "cron": "0 9 * * *",
        "human": "9:00 AM daily",
        "anti_flap_window": 7,
        "default_stagnation_threshold": 7,
        "process_evolve_requests": True,
        "idle_window": idle_window,
        "gha_minutes_cap": gha_minutes_cap,
        "kill_switch": kill_switch,
    }
    if idle_window_tz is not None:
        meta["idle_window_tz"] = idle_window_tz
    return {
        "schema_version": 4,
        "repo_slug": "demo",
        "goal": "ship v1",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": routines,
        "neutralized_tasks": [],
        "meta": meta,
    }


def make_state(
    *,
    used: int = 0,
    reset_date: str = "2026-05-10",
    last_event_id: int = 0,
    kill_switch_active: bool = False,
    last_dispatch: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "gha_minutes_used_today": used,
        "gha_minutes_reset_date": reset_date,
        "last_event_id": last_event_id,
        "kill_switch_active": kill_switch_active,
        "last_dispatch": last_dispatch or {},
    }


def at(tz: str, *, year=2026, month=5, day=10, hour=14, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_local_routine_fires(self, orch):
        cfg = make_config(routines=[make_routine(surface="local")])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert len(out["decisions"]) == 1
        d = out["decisions"][0]
        assert d["routine_id"] == "pr-watcher"
        assert d["action"] == "fire"
        assert d["surface"] == "local"

    def test_gha_routine_fires_when_idle_is_always(self, orch):
        cfg = make_config(routines=[make_routine(surface="gha")])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert out["decisions"][0]["action"] == "fire"
        assert out["decisions"][0]["surface"] == "gha"

    def test_returns_decisions_and_new_state(self, orch):
        cfg = make_config(routines=[make_routine()])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert "decisions" in out
        assert "new_state" in out
        assert isinstance(out["decisions"], list)
        assert isinstance(out["new_state"], dict)


# ---------------------------------------------------------------------------
# Purity — never mutates inputs
# ---------------------------------------------------------------------------

class TestPurity:
    def test_does_not_mutate_state(self, orch):
        state = make_state()
        snapshot = copy.deepcopy(state)
        cfg = make_config(routines=[make_routine(surface="gha")])
        orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert state == snapshot, "tick mutated state"

    def test_does_not_mutate_config(self, orch):
        cfg = make_config(routines=[make_routine()])
        snapshot = copy.deepcopy(cfg)
        orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert cfg == snapshot, "tick mutated config"

    def test_does_not_mutate_candidates(self, orch):
        cfg = make_config(routines=[make_routine()])
        candidates = cfg["routines"]
        snapshot = copy.deepcopy(candidates)
        orch.tick(at("UTC"), candidates, make_state(), cfg)
        assert candidates == snapshot, "tick mutated candidates"


# ---------------------------------------------------------------------------
# Skip — kill switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_meta_kill_switch_skips_everything(self, orch):
        """meta.kill_switch is the user-facing toggle. When True, NO
        routine fires, regardless of surface."""
        cfg = make_config(
            routines=[
                make_routine("a", surface="local"),
                make_routine("b", surface="gha"),
            ],
            kill_switch=True,
        )
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert len(out["decisions"]) == 2
        for d in out["decisions"]:
            assert d["action"] == "skip"
            assert "kill" in d["reason"].lower()

    def test_state_kill_switch_active_also_skips(self, orch):
        """state.json's kill_switch_active is the fast-read mirror — if
        EITHER is True, the orchestrator halts dispatch."""
        cfg = make_config(routines=[make_routine()], kill_switch=False)
        state = make_state(kill_switch_active=True)
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert out["decisions"][0]["action"] == "skip"
        assert "kill" in out["decisions"][0]["reason"].lower()


# ---------------------------------------------------------------------------
# Skip — non-firing state
# ---------------------------------------------------------------------------

class TestRoutineState:
    @pytest.mark.parametrize(
        "state", ["PROPOSED", "STAGNANT", "COMPLETED", "STOPPED"],
    )
    def test_non_firing_state_skipped(self, orch, state):
        cfg = make_config(routines=[make_routine(state=state)])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert out["decisions"][0]["action"] == "skip"
        assert state.lower() in out["decisions"][0]["reason"].lower() or \
            "state" in out["decisions"][0]["reason"].lower()

    @pytest.mark.parametrize("state", ["ACTIVE", "EVOLVING"])
    def test_firing_states_dispatch(self, orch, state):
        cfg = make_config(routines=[make_routine(state=state)])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        assert out["decisions"][0]["action"] == "fire"


# ---------------------------------------------------------------------------
# Skip — idle window (GHA only)
# ---------------------------------------------------------------------------

class TestIdleWindow:
    def test_gha_routine_skipped_inside_idle_window(self, orch):
        cfg = make_config(
            routines=[make_routine(surface="gha")],
            idle_window="22:00-08:00",
            idle_window_tz="America/Los_Angeles",
        )
        # 23:00 PT — inside the window
        now = at("America/Los_Angeles", hour=23)
        out = orch.tick(now, cfg["routines"], make_state(), cfg)
        d = out["decisions"][0]
        assert d["action"] == "skip"
        assert "idle" in d["reason"].lower()

    def test_local_routine_unaffected_by_idle_window(self, orch):
        """Idle window only constrains GHA dispatch — local routines run
        in the user's session and aren't subject to the cost ceiling."""
        cfg = make_config(
            routines=[make_routine(surface="local")],
            idle_window="22:00-08:00",
            idle_window_tz="America/Los_Angeles",
        )
        now = at("America/Los_Angeles", hour=23)
        out = orch.tick(now, cfg["routines"], make_state(), cfg)
        assert out["decisions"][0]["action"] == "fire"

    def test_gha_routine_fires_outside_idle_window(self, orch):
        cfg = make_config(
            routines=[make_routine(surface="gha")],
            idle_window="22:00-08:00",
            idle_window_tz="America/Los_Angeles",
        )
        now = at("America/Los_Angeles", hour=14)  # 2pm — outside
        out = orch.tick(now, cfg["routines"], make_state(), cfg)
        assert out["decisions"][0]["action"] == "fire"


# ---------------------------------------------------------------------------
# Skip — cost cap (GHA only)
# ---------------------------------------------------------------------------

class TestCostCap:
    def test_gha_skipped_when_cap_would_exceed(self, orch):
        cfg = make_config(
            routines=[make_routine(surface="gha", est_minutes=10)],
            gha_minutes_cap=60,
        )
        state = make_state(used=55)  # 55 + 10 = 65 > 60
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        d = out["decisions"][0]
        assert d["action"] == "skip"
        assert "cap" in d["reason"].lower() or "cost" in d["reason"].lower()

    def test_gha_fires_when_cap_has_room(self, orch):
        cfg = make_config(
            routines=[make_routine(surface="gha", est_minutes=5)],
            gha_minutes_cap=60,
        )
        state = make_state(used=50)
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert out["decisions"][0]["action"] == "fire"

    def test_local_routine_ignores_cost_cap(self, orch):
        """Cost cap only applies to GHA — local fires regardless."""
        cfg = make_config(
            routines=[make_routine(surface="local", est_minutes=10)],
            gha_minutes_cap=60,
        )
        state = make_state(used=999)  # already way over
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert out["decisions"][0]["action"] == "fire"


# ---------------------------------------------------------------------------
# State update
# ---------------------------------------------------------------------------

class TestStateUpdate:
    def test_event_id_increments_per_tick(self, orch):
        cfg = make_config(routines=[make_routine()])
        state = make_state(last_event_id=42)
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert out["new_state"]["last_event_id"] == 43

    def test_event_id_increments_even_when_all_skipped(self, orch):
        """Every tick is observable — counter advances even if nothing
        dispatched. The dashboard uses this to detect 'orchestrator alive'."""
        cfg = make_config(routines=[make_routine()], kill_switch=True)
        state = make_state(last_event_id=42)
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert out["new_state"]["last_event_id"] == 43

    def test_gha_used_increases_only_for_gha_fires(self, orch):
        cfg = make_config(
            routines=[
                make_routine("a", surface="gha", est_minutes=7),
                make_routine("b", surface="local", est_minutes=99),
            ],
        )
        out = orch.tick(at("UTC"), cfg["routines"], make_state(used=10), cfg)
        # a fires on gha (+7), b fires local (no charge)
        assert out["new_state"]["gha_minutes_used_today"] == 17

    def test_used_does_not_increase_for_skipped_gha(self, orch):
        cfg = make_config(
            routines=[make_routine(surface="gha", est_minutes=7)],
            kill_switch=True,
        )
        out = orch.tick(at("UTC"), cfg["routines"], make_state(used=10), cfg)
        assert out["new_state"]["gha_minutes_used_today"] == 10

    def test_resets_cost_when_day_flipped(self, orch):
        cfg = make_config(
            routines=[make_routine(surface="gha", est_minutes=5)],
            idle_window="always",
            gha_minutes_cap=60,
        )
        # State says yesterday; now is today → reset before counting
        state = make_state(used=55, reset_date="2026-05-09")
        now = at("UTC", year=2026, month=5, day=10, hour=12)
        out = orch.tick(now, cfg["routines"], state, cfg)
        # Reset wipes the 55, then this fire adds 5
        assert out["new_state"]["gha_minutes_used_today"] == 5
        assert out["new_state"]["gha_minutes_reset_date"] == "2026-05-10"
        # And the routine fires (it would have been over cap before reset)
        assert out["decisions"][0]["action"] == "fire"

    def test_reset_uses_idle_window_tz_for_date(self, orch):
        """At 02:00 UTC on 2026-05-11, it's still 2026-05-10 in LA. The
        reset must NOT fire if the user is in LA tz."""
        cfg = make_config(
            routines=[make_routine(surface="gha")],
            idle_window="always",  # avoid idle-window interference
            idle_window_tz="America/Los_Angeles",
        )
        state = make_state(used=10, reset_date="2026-05-10")
        now = at("UTC", year=2026, month=5, day=11, hour=2)
        out = orch.tick(now, cfg["routines"], state, cfg)
        assert out["new_state"]["gha_minutes_reset_date"] == "2026-05-10"

    def test_last_dispatch_records_fired_routine(self, orch):
        cfg = make_config(routines=[make_routine(surface="gha", est_minutes=5)])
        state = make_state()
        out = orch.tick(at("UTC", hour=14), cfg["routines"], state, cfg)
        ld = out["new_state"]["last_dispatch"]
        assert "pr-watcher" in ld
        rec = ld["pr-watcher"]
        assert rec["surface"] == "gha"
        assert rec["outcome"] == "ok"
        # ts is local ISO 8601 with offset (no UTC `Z`)
        assert "Z" not in rec["ts"]
        assert rec["ts"].count("T") == 1

    def test_last_dispatch_unchanged_for_skipped_routines(self, orch):
        """Skipped routines keep their previous last_dispatch entry."""
        prev = {
            "pr-watcher": {
                "ts": "2026-05-09T17:03:00-0700",
                "surface": "gha",
                "outcome": "ok",
            }
        }
        cfg = make_config(routines=[make_routine(surface="gha")], kill_switch=True)
        state = make_state(last_dispatch=copy.deepcopy(prev))
        out = orch.tick(at("UTC"), cfg["routines"], state, cfg)
        assert out["new_state"]["last_dispatch"] == prev

    def test_new_state_passes_state_validator(self, orch):
        """After a tick, new_state must remain a valid state.json."""
        # Load state validator from scripts/state.py
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "state_schema_for_tick_test", ROOT / "scripts" / "state.py"
        )
        state_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(state_mod)

        cfg = make_config(
            routines=[
                make_routine("a", surface="gha", est_minutes=5),
                make_routine("b", surface="local"),
                make_routine("c", state="STAGNANT"),
            ]
        )
        out = orch.tick(at("UTC", hour=14), cfg["routines"], make_state(), cfg)
        errors = state_mod.validate_state(out["new_state"])
        assert errors == [], errors


# ---------------------------------------------------------------------------
# Mixed candidate batches — ordering, independence
# ---------------------------------------------------------------------------

class TestMixedBatches:
    def test_decision_order_matches_candidate_order(self, orch):
        cfg = make_config(routines=[
            make_routine("z-last"),
            make_routine("a-first"),
            make_routine("m-mid"),
        ])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        ids = [d["routine_id"] for d in out["decisions"]]
        assert ids == ["z-last", "a-first", "m-mid"]

    def test_one_routines_skip_does_not_affect_another(self, orch):
        cfg = make_config(routines=[
            make_routine("alive", surface="gha", est_minutes=5),
            make_routine("dead", state="STOPPED", surface="gha", est_minutes=99),
        ])
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        ds = {d["routine_id"]: d for d in out["decisions"]}
        assert ds["alive"]["action"] == "fire"
        assert ds["dead"]["action"] == "skip"

    def test_cap_consumed_in_candidate_order(self, orch):
        """Two GHA routines each estimating 40min; cap=60. First fires
        (uses 40), second is skipped (40+40 > 60). This makes 'cap' a
        first-come-first-served budget — predictable, deterministic."""
        cfg = make_config(
            routines=[
                make_routine("first", surface="gha", est_minutes=40),
                make_routine("second", surface="gha", est_minutes=40),
            ],
            gha_minutes_cap=60,
        )
        out = orch.tick(at("UTC"), cfg["routines"], make_state(), cfg)
        ds = {d["routine_id"]: d for d in out["decisions"]}
        assert ds["first"]["action"] == "fire"
        assert ds["second"]["action"] == "skip"
        assert "cap" in ds["second"]["reason"].lower() or "cost" in ds["second"]["reason"].lower()
