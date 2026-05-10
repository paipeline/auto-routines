#!/usr/bin/env python3
"""
state.py — schema + validator for `.iteration/state.json`.

state.json is the runtime ledger that the orchestrator reads on every tick
and that GHA workflows (and local routines) write after each dispatch.
Two writers means we MUST fail-loud on schema drift; that's the whole job
of `validate_state`.

Public surface (kept tiny so the orchestrator + install scripts can both
import it without dragging anything else in):

  STATE_SCHEMA_VERSION   pinned constant (currently 1)
  validate_state(s)      pure function (dict -> list of error strings)
  initial_state(...)     convenience constructor for `init`

The validator does no I/O — callers are responsible for reading and
writing the file. That keeps unit tests deterministic and makes the
orchestrator trivially testable with hand-built dicts.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any


STATE_SCHEMA_VERSION = 1

# Surfaces a routine can fire on. Mirrors EXECUTION_SURFACES in
# scripts/sanity-check.py — kept duplicated here so this module has zero
# imports from the rest of the repo (the orchestrator imports both, and a
# circular dep would be annoying). If you change one, change both.
DISPATCH_SURFACES = {"gha", "local"}
DISPATCH_OUTCOMES = {"ok", "noop", "warn", "err"}

# kebab-case routine id — same regex as sanity-check.py.
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# YYYY-MM-DD (zero-padded) — used for the GHA-minutes reset boundary.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Local-time ISO 8601 with offset, e.g. 2026-05-10T17:03:00-0700.
# The trailing offset MUST be ±HHMM (no `Z`) — UTC `Z` is banned per
# SKILL.md / routine-skill.md (logs are read on the user's local machine).
_LOCAL_ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$"
)


def _is_strict_int(v: Any) -> bool:
    """isinstance(True, int) is True in Python. We never want bools where
    we asked for ints, so reject them explicitly."""
    return isinstance(v, int) and not isinstance(v, bool)


def _is_real_iso_date(s: str) -> bool:
    if not _ISO_DATE.match(s):
        return False
    try:
        _dt.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def validate_state(state: Any) -> list[str]:
    """Return a list of error strings; empty list = valid.

    Pure function: no I/O, no logging, no side-effects. Caller decides
    what to do with errors (the orchestrator halts; install rewrites
    state.json from `initial_state()`)."""
    errors: list[str] = []

    if not isinstance(state, dict):
        return [f"state must be a JSON object/dict, got {type(state).__name__}"]

    required = (
        "schema_version",
        "gha_minutes_used_today",
        "gha_minutes_reset_date",
        "last_event_id",
        "kill_switch_active",
        "last_dispatch",
    )
    for k in required:
        if k not in state:
            errors.append(f"missing top-level key: {k}")
    if errors:
        # Stop here — the rest of the checks would just produce noise.
        return errors

    sv = state["schema_version"]
    # Strict int — `1.0 == 1` is True in Python, but a float in the file is
    # almost always a sign of corruption (json.dump would have written `1`).
    if not _is_strict_int(sv) or sv != STATE_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be int {STATE_SCHEMA_VERSION} "
            f"(this validator is pinned), got {sv!r}"
        )

    used = state["gha_minutes_used_today"]
    if not _is_strict_int(used) or used < 0:
        errors.append(
            f"gha_minutes_used_today must be a non-negative int, got {used!r}"
        )

    reset_date = state["gha_minutes_reset_date"]
    if not isinstance(reset_date, str) or not _is_real_iso_date(reset_date):
        errors.append(
            f"gha_minutes_reset_date must be an ISO date 'YYYY-MM-DD' "
            f"(in idle_window_tz), got {reset_date!r}"
        )

    eid = state["last_event_id"]
    if not _is_strict_int(eid) or eid < 0:
        errors.append(
            f"last_event_id must be a non-negative int, got {eid!r}"
        )

    if not isinstance(state["kill_switch_active"], bool):
        errors.append(
            f"kill_switch_active must be a bool, got {state['kill_switch_active']!r}"
        )

    last = state["last_dispatch"]
    if not isinstance(last, dict):
        errors.append(
            f"last_dispatch must be a dict mapping routine_id -> record, "
            f"got {type(last).__name__}"
        )
    else:
        for rid, rec in last.items():
            errors.extend(_validate_dispatch_record(rid, rec))

    return errors


def _validate_dispatch_record(rid: Any, rec: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(rid, str) or not _KEBAB.match(rid):
        errs.append(
            f"last_dispatch key {rid!r} must be a kebab-case routine id"
        )
        # Even with a bad key, still validate the record so we surface
        # multiple mistakes in one pass (better DX than fix-one-at-a-time).
    if not isinstance(rec, dict):
        errs.append(
            f"last_dispatch[{rid!r}] must be a dict, got {type(rec).__name__}"
        )
        return errs
    for k in ("ts", "surface", "outcome"):
        if k not in rec:
            errs.append(f"last_dispatch[{rid!r}] missing key: {k}")
    if "ts" in rec:
        ts = rec["ts"]
        if not isinstance(ts, str) or not _LOCAL_ISO_TS.match(ts):
            errs.append(
                f"last_dispatch[{rid!r}].ts must be local ISO 8601 with "
                f"±HHMM offset (not UTC 'Z'), got {ts!r}"
            )
    if "surface" in rec:
        s = rec["surface"]
        if s not in DISPATCH_SURFACES:
            errs.append(
                f"last_dispatch[{rid!r}].surface must be one of "
                f"{sorted(DISPATCH_SURFACES)}, got {s!r}"
            )
    if "outcome" in rec:
        o = rec["outcome"]
        if o not in DISPATCH_OUTCOMES:
            errs.append(
                f"last_dispatch[{rid!r}].outcome must be one of "
                f"{sorted(DISPATCH_OUTCOMES)}, got {o!r}"
            )
    return errs


def initial_state(reset_date: str) -> dict:
    """Build a fresh state.json that passes the validator.

    `reset_date` should be today's date in the configured idle_window_tz —
    callers compute it from `meta.idle_window_tz` and pass it in (this
    module deliberately knows nothing about config.yaml)."""
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "gha_minutes_used_today": 0,
        "gha_minutes_reset_date": reset_date,
        "last_event_id": 0,
        "kill_switch_active": False,
        "last_dispatch": {},
    }
