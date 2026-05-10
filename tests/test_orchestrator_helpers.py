"""
Helper-function tests for scripts/orchestrator.py (PRD #10 Module 1, phase 1).

The orchestrator's `tick()` composes four small pure decisions:

  is_in_idle_window(now, idle_window, tz)   -> bool   ('don't fire GHA work')
  should_reset_cost(now, reset_date, tz)    -> bool   ('roll the daily counter')
  would_exceed_cap(used, est, cap)          -> bool   ('skip — cap hit')
  is_firing_state(routine_state)            -> bool   ('routine is dispatchable')

Testing these in isolation keeps the tick() tests focused on composition
rather than re-asserting time-zone math five times.

All helpers are PURE — they take primitive inputs and return primitive
outputs. No I/O, no `datetime.now()`, no globals. The orchestrator passes
a frozen `now` from the trigger.
"""
from __future__ import annotations

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
    assert spec and spec.loader, "scripts/orchestrator.py must exist"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return _load_orchestrator()


# Convenience: build a tz-aware datetime in a named zone.
def _at(tz: str, *, year=2026, month=5, day=10, hour=0, minute=0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


# ---------------------------------------------------------------------------
# is_in_idle_window
# ---------------------------------------------------------------------------

class TestIsInIdleWindow:
    def test_always_means_never_idle(self, orch):
        """idle_window == 'always' is the schema-v4 opt-out: the
        orchestrator can fire GHA work at any time."""
        for hour in (0, 6, 12, 18, 23):
            now = _at("UTC", hour=hour)
            assert orch.is_in_idle_window(now, "always", "UTC") is False

    def test_simple_daytime_window(self, orch):
        """09:00-17:00 — clear daytime range, no midnight wrap."""
        tz = "America/Los_Angeles"
        # inside
        assert orch.is_in_idle_window(_at(tz, hour=9), "09:00-17:00", tz) is True
        assert orch.is_in_idle_window(_at(tz, hour=12), "09:00-17:00", tz) is True
        # boundary: end is exclusive (17:00 sharp = first minute outside)
        assert orch.is_in_idle_window(_at(tz, hour=17), "09:00-17:00", tz) is False
        # outside
        assert orch.is_in_idle_window(_at(tz, hour=8, minute=59), "09:00-17:00", tz) is False
        assert orch.is_in_idle_window(_at(tz, hour=20), "09:00-17:00", tz) is False

    def test_overnight_wrap_window(self, orch):
        """22:00-08:00 wraps midnight — both 23:00 and 03:00 are inside."""
        tz = "America/Los_Angeles"
        assert orch.is_in_idle_window(_at(tz, hour=22), "22:00-08:00", tz) is True
        assert orch.is_in_idle_window(_at(tz, hour=23), "22:00-08:00", tz) is True
        assert orch.is_in_idle_window(_at(tz, hour=0), "22:00-08:00", tz) is True
        assert orch.is_in_idle_window(_at(tz, hour=3), "22:00-08:00", tz) is True
        assert orch.is_in_idle_window(_at(tz, hour=7, minute=59), "22:00-08:00", tz) is True
        # End boundary is exclusive
        assert orch.is_in_idle_window(_at(tz, hour=8), "22:00-08:00", tz) is False
        assert orch.is_in_idle_window(_at(tz, hour=12), "22:00-08:00", tz) is False

    def test_uses_idle_window_tz_not_input_tz(self, orch):
        """`now` is whatever zone the trigger captured (often UTC). The
        helper must convert it to idle_window_tz before comparing — that's
        the bug PRD #10's reviewer flagged ('silent UTC fallback'). Here:
        02:00 UTC = 19:00 PT the previous day → outside a 22:00-08:00 PT
        window."""
        # 02:00 UTC = 18:00 or 19:00 PT (depending on DST). May 10 is PDT
        # (UTC-7), so 02:00 UTC = 19:00 PDT — outside 22:00-08:00.
        now_utc = _at("UTC", hour=2)
        assert orch.is_in_idle_window(
            now_utc, "22:00-08:00", "America/Los_Angeles"
        ) is False
        # Same now (02:00 UTC) interpreted in Tokyo = 11:00 JST → outside
        # a 22:00-08:00 JST window.
        assert orch.is_in_idle_window(
            now_utc, "22:00-08:00", "Asia/Tokyo"
        ) is False

    def test_returns_bool_not_truthy(self, orch):
        """The orchestrator branches on this — a non-bool would silently
        widen the API surface."""
        result = orch.is_in_idle_window(_at("UTC"), "always", "UTC")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# should_reset_cost
# ---------------------------------------------------------------------------

class TestShouldResetCost:
    def test_same_day_no_reset(self, orch):
        """If today (in idle_window_tz) matches the stored reset_date,
        the counter holds."""
        now = _at("America/Los_Angeles", year=2026, month=5, day=10, hour=14)
        assert orch.should_reset_cost(now, "2026-05-10", "America/Los_Angeles") is False

    def test_next_day_resets(self, orch):
        now = _at("America/Los_Angeles", year=2026, month=5, day=11, hour=0, minute=1)
        assert orch.should_reset_cost(now, "2026-05-10", "America/Los_Angeles") is True

    def test_uses_idle_window_tz_for_date_boundary(self, orch):
        """02:00 UTC on 2026-05-11 is still 2026-05-10 in Los Angeles
        (UTC-7) — the reset must NOT fire yet."""
        now_utc = _at("UTC", year=2026, month=5, day=11, hour=2)
        assert orch.should_reset_cost(
            now_utc, "2026-05-10", "America/Los_Angeles"
        ) is False
        # But that same instant is 2026-05-11 in Tokyo (UTC+9) → 11:00 JST
        # → reset SHOULD fire.
        assert orch.should_reset_cost(
            now_utc, "2026-05-10", "Asia/Tokyo"
        ) is True

    def test_clock_skew_backward_no_reset(self, orch):
        """If `now` is before reset_date (clock skew, manual edit), refuse
        to reset — that would zero a still-accumulating window."""
        now = _at("America/Los_Angeles", year=2026, month=5, day=9)
        assert orch.should_reset_cost(now, "2026-05-10", "America/Los_Angeles") is False


# ---------------------------------------------------------------------------
# would_exceed_cap
# ---------------------------------------------------------------------------

class TestWouldExceedCap:
    @pytest.mark.parametrize(
        "used,est,cap,expected",
        [
            (0, 5, 60, False),      # plenty of room
            (50, 5, 60, False),     # exactly fits up to cap
            (55, 5, 60, False),     # fits exactly
            (56, 5, 60, True),      # would exceed by 1
            (60, 1, 60, True),      # already at cap
            (100, 5, 60, True),     # already over (shouldn't happen, but be safe)
            (0, 60, 60, False),     # single huge job that fits
            (0, 61, 60, True),      # single job too big
        ],
    )
    def test_cap_arithmetic(self, orch, used, est, cap, expected):
        assert orch.would_exceed_cap(used, est, cap) is expected

    def test_returns_bool(self, orch):
        assert isinstance(orch.would_exceed_cap(0, 5, 60), bool)


# ---------------------------------------------------------------------------
# is_firing_state
# ---------------------------------------------------------------------------

class TestIsFiringState:
    @pytest.mark.parametrize("state", ["ACTIVE", "EVOLVING"])
    def test_firing_states(self, orch, state):
        """Per sanity-check.py FSM: only ACTIVE + EVOLVING dispatch."""
        assert orch.is_firing_state(state) is True

    @pytest.mark.parametrize(
        "state", ["PROPOSED", "STAGNANT", "COMPLETED", "STOPPED"],
    )
    def test_non_firing_states(self, orch, state):
        assert orch.is_firing_state(state) is False

    @pytest.mark.parametrize("bogus", ["active", "Active", "RUNNING", "", None, 42])
    def test_invalid_state_is_not_firing(self, orch, bogus):
        """Defensive: an unrecognized state should not fire — it'd just
        cascade into more trouble downstream."""
        assert orch.is_firing_state(bogus) is False


# ---------------------------------------------------------------------------
# Constants exposed for the orchestrator + its tests
# ---------------------------------------------------------------------------

def test_module_exposes_firing_states_constant(orch):
    """Pin the set so the orchestrator and sanity-check stay in lockstep."""
    assert orch.FIRING_STATES == frozenset({"ACTIVE", "EVOLVING"})
