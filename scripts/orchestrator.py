#!/usr/bin/env python3
"""
orchestrator.py — the deep module behind PRD #10 Module 1.

Composes a single pure decision per tick:

    tick(trigger, state, config) -> DispatchDecision

`tick()` itself will be added in phase 2. This file currently exposes the
small pure helpers it composes — keeping them here (and not in
sanity-check.py) keeps the validator's surface tight while still letting
the orchestrator's tests assert each rule in isolation.

Design rules:
- No I/O, no `datetime.now()`, no globals. Callers always pass `now`.
- All datetime inputs are tz-aware. We refuse naive datetimes loudly.
- `idle_window_tz` is the source of truth for clock comparisons —
  PRD #10 review specifically called out 'silent UTC fallback' as a
  footgun, so we always convert.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Mirrors sanity.FIRING_STATES — duplicated here so the orchestrator
# has zero imports from the rest of the repo (the test pins both).
FIRING_STATES: frozenset = frozenset({"ACTIVE", "EVOLVING"})

_IDLE_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def _to_zone(now: dt.datetime, tz_name: str) -> dt.datetime:
    """Convert a tz-aware datetime to `tz_name`. Refuse naive datetimes
    loudly — silent UTC interpretation was the bug."""
    if now.tzinfo is None:
        raise ValueError(
            "orchestrator helpers require tz-aware datetimes "
            "(pass `now` with a tzinfo, e.g. ZoneInfo('UTC'))"
        )
    try:
        target = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"invalid IANA timezone: {tz_name!r}") from e
    return now.astimezone(target)


def is_in_idle_window(now: dt.datetime, idle_window: str, tz_name: str) -> bool:
    """True iff `now` (converted to `tz_name`) falls inside `idle_window`.

    `idle_window` is either:
      - "always"     — schema-v4 opt-out (always returns False — never idle)
      - "HH:MM-HH:MM" — clock range; END is exclusive; range may wrap midnight

    We deliberately treat the END as exclusive so adjacent windows
    (e.g. one routine 09:00-13:00, another 13:00-17:00) don't both fire
    at 13:00 sharp. Same convention as cron-style schedule boundaries.
    """
    if idle_window == "always":
        return False
    if not _IDLE_WINDOW_RE.match(idle_window):
        raise ValueError(
            f"idle_window must be 'always' or 'HH:MM-HH:MM', got {idle_window!r}"
        )
    local = _to_zone(now, tz_name)
    start_str, end_str = idle_window.split("-")
    sh, sm = (int(x) for x in start_str.split(":"))
    eh, em = (int(x) for x in end_str.split(":"))
    cur_minutes = local.hour * 60 + local.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    if start_minutes < end_minutes:
        # Normal range, e.g. 09:00-17:00
        return start_minutes <= cur_minutes < end_minutes
    if start_minutes > end_minutes:
        # Wraps midnight, e.g. 22:00-08:00 → in if >=22:00 OR <08:00
        return cur_minutes >= start_minutes or cur_minutes < end_minutes
    # start == end — degenerate, treat as never-idle (zero-length window)
    return False


def should_reset_cost(
    now: dt.datetime, reset_date: str, tz_name: str
) -> bool:
    """True iff today (in `tz_name`) is strictly after `reset_date`.

    `reset_date` is the ISO date the daily GHA-minute counter was last
    rolled. A tick that lands on the next local day rolls the counter
    back to zero (caller's responsibility — this helper only signals).

    Returns False on clock skew (now < reset_date) so we don't zero an
    in-progress window."""
    try:
        stored = dt.date.fromisoformat(reset_date)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"reset_date must be ISO 'YYYY-MM-DD', got {reset_date!r}"
        ) from e
    today = _to_zone(now, tz_name).date()
    return today > stored


def would_exceed_cap(used: int, est: int, cap: int) -> bool:
    """True iff dispatching a routine of `est` minutes would push the
    daily total past `cap`. Used by the orchestrator before each GHA fire.

    Comparison is `used + est > cap` so a routine that EXACTLY fills the
    remaining budget still fires (it just leaves zero headroom)."""
    return (used + est) > cap


def is_firing_state(state: Any) -> bool:
    """True iff a routine in this FSM state is allowed to dispatch.

    Defensive: any non-string or unrecognized state returns False rather
    than raising — the orchestrator should skip-with-reason, not crash."""
    return state in FIRING_STATES


# ---------------------------------------------------------------------------
# tick() — the dispatch decision
# ---------------------------------------------------------------------------

# Routines that always run inside the user's session — surface is fixed.
_SESSION_PRIMITIVES = frozenset({"hook", "git-hook", "loop"})


def _local_iso_with_offset(now: dt.datetime) -> str:
    """Format a tz-aware datetime as `2026-05-10T17:03:00-0700`.
    Matches the rule pinned in routine-skill.md and state.py: never UTC `Z`."""
    if now.tzinfo is None:
        raise ValueError("tick requires a tz-aware `now`")
    # %z → ±HHMM; strftime for the rest. Don't use isoformat(): it emits
    # `+00:00` instead of `+0000` and our state validator rejects that.
    return now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _surface_for(routine: dict) -> str:
    """Resolve the surface a routine fires on. hook/git-hook/loop always
    run in-session (treated as 'local' for state-tracking purposes).
    Scheduled and pr-poll must declare execution_surface; we trust the
    sanity-checker has already enforced that."""
    if routine.get("primitive") in _SESSION_PRIMITIVES:
        return "local"
    return routine.get("execution_surface", "local")


def tick(
    now: dt.datetime,
    candidates: list[dict],
    state: dict,
    config: dict,
) -> dict:
    """Decide which candidate routines to dispatch on this tick.

    Inputs:
      now         — tz-aware datetime captured from the trigger.
      candidates  — routine dicts the trigger layer has filtered to fire.
      state       — current state.json (must already validate).
      config      — current config.yaml (must already validate, schema 4+).

    Output:
      {
        "decisions": [{routine_id, action, surface, reason}, ...],
        "new_state": <updated state dict>,
      }

    `decisions` preserves the order of `candidates`. Cost cap is consumed
    first-come-first-served — first routine to fit the remaining budget
    fires; latecomers are skipped with reason 'cost cap'.

    Pure: never mutates inputs. Calls no I/O. Idempotent for a given
    (now, candidates, state, config).
    """
    if now.tzinfo is None:
        raise ValueError("tick requires a tz-aware `now`")

    meta = config.get("meta", {})
    idle_window = meta.get("idle_window", "always")
    idle_window_tz = meta.get("idle_window_tz")
    gha_cap = meta.get("gha_minutes_cap", 60)
    config_kill = bool(meta.get("kill_switch", False))

    # Snapshot state so we never touch the caller's dict.
    new_state = {
        **state,
        "last_dispatch": dict(state.get("last_dispatch", {})),
    }
    new_state["last_event_id"] = state.get("last_event_id", 0) + 1

    # Roll the daily counter if we crossed midnight in idle_window_tz.
    # Skip when 'always' (no tz set) or no tz declared — no daily concept
    # to anchor against; counter just keeps accumulating until evolve
    # rolls it. (The schema layer enforces tz when window != 'always',
    # so this branch is the safe degenerate case.)
    reset_tz = idle_window_tz or "UTC"
    try:
        if should_reset_cost(now, state.get("gha_minutes_reset_date", ""), reset_tz):
            new_state["gha_minutes_used_today"] = 0
            new_state["gha_minutes_reset_date"] = (
                _to_zone(now, reset_tz).date().isoformat()
            )
    except ValueError:
        # Bad reset_date — leave counter alone; the validator will catch
        # this on the next state-write round-trip.
        pass

    state_kill = bool(state.get("kill_switch_active", False))
    kill_active = config_kill or state_kill

    decisions: list[dict] = []
    for routine in candidates:
        rid = routine.get("id", "?")
        # 1. Kill switch — short-circuit everything.
        if kill_active:
            decisions.append({
                "routine_id": rid,
                "action": "skip",
                "surface": None,
                "reason": "kill switch active",
            })
            continue
        # 2. FSM state — only ACTIVE/EVOLVING dispatch.
        rstate = routine.get("state", "ACTIVE")
        if not is_firing_state(rstate):
            decisions.append({
                "routine_id": rid,
                "action": "skip",
                "surface": None,
                "reason": f"state={rstate}",
            })
            continue
        # 3. Surface routing.
        surface = _surface_for(routine)
        # 4. GHA-only restrictions: idle window, cost cap.
        if surface == "gha":
            try:
                idle = is_in_idle_window(now, idle_window, reset_tz)
            except ValueError:
                # Malformed idle_window in config — treat as 'never idle'
                # (sanity check should have caught this; don't crash).
                idle = False
            if idle:
                decisions.append({
                    "routine_id": rid,
                    "action": "skip",
                    "surface": None,
                    "reason": f"in idle window ({idle_window} {reset_tz})",
                })
                continue
            est = routine.get("est_minutes", 5)
            current_used = new_state["gha_minutes_used_today"]
            if would_exceed_cap(current_used, est, gha_cap):
                decisions.append({
                    "routine_id": rid,
                    "action": "skip",
                    "surface": None,
                    "reason": (
                        f"cost cap reached: {current_used}+{est} > {gha_cap}"
                    ),
                })
                continue
            new_state["gha_minutes_used_today"] = current_used + est
        # 5. Fire — record dispatch.
        decisions.append({
            "routine_id": rid,
            "action": "fire",
            "surface": surface,
            "reason": "ok",
        })
        new_state["last_dispatch"][rid] = {
            "ts": _local_iso_with_offset(now),
            "surface": surface,
            "outcome": "ok",
        }

    return {"decisions": decisions, "new_state": new_state}
