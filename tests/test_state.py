"""
Schema tests for .iteration/state.json (PRD #10 Module 5, Phase 2).

state.json is the runtime ledger the orchestrator (Module 1) reads on every
tick and that GHA workflows write after each dispatch. The validator's job
is to fail-loud when:
  - a partial write left the file truncated,
  - a schema drift between local and GHA writers corrupts a record,
  - the user hand-edited the file into a bad shape.

The validator is pure (state dict → list of error strings); it does no I/O,
which keeps the orchestrator's tests deterministic.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_state_module():
    """Import scripts/state.py with its hyphenated path. Mirrors the trick
    in tests/conftest.py for sanity-check.py."""
    spec = importlib.util.spec_from_file_location(
        "state_schema", ROOT / "scripts" / "state.py"
    )
    assert spec and spec.loader, "scripts/state.py must exist"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["state_schema"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def state_mod():
    return _load_state_module()


@pytest.fixture
def base_state() -> dict:
    """Minimal valid state.json — what the orchestrator writes after init."""
    return {
        "schema_version": 1,
        "gha_minutes_used_today": 0,
        "gha_minutes_reset_date": "2026-05-10",
        "last_event_id": 0,
        "kill_switch_active": False,
        "last_dispatch": {},
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_minimal_state_passes(state_mod, base_state):
    assert state_mod.validate_state(base_state) == []


def test_state_schema_version_constant(state_mod):
    """Pin the version — GHA workflows and local routines read this to
    decide whether to migrate or refuse."""
    assert state_mod.STATE_SCHEMA_VERSION == 1


def test_state_with_dispatch_records_passes(state_mod, base_state):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": "2026-05-10T17:03:00-0700",
            "surface": "gha",
            "outcome": "ok",
        },
        "daily-digest": {
            "ts": "2026-05-10T18:00:00-0700",
            "surface": "local",
            "outcome": "noop",
        },
    }
    assert state_mod.validate_state(base_state) == []


# ---------------------------------------------------------------------------
# Required keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing",
    [
        "schema_version",
        "gha_minutes_used_today",
        "gha_minutes_reset_date",
        "last_event_id",
        "kill_switch_active",
        "last_dispatch",
    ],
)
def test_state_missing_top_level_key_fails(state_mod, base_state, missing):
    del base_state[missing]
    errors = state_mod.validate_state(base_state)
    assert any(missing in e for e in errors), errors


def test_state_must_be_a_dict(state_mod):
    for v in [None, [], "x", 42]:
        errors = state_mod.validate_state(v)
        assert errors, f"non-dict value {v!r} should fail"


# ---------------------------------------------------------------------------
# schema_version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v", [0, -1, 2, "1", 1.0, None])
def test_state_schema_version_must_match_constant(state_mod, base_state, v):
    base_state["schema_version"] = v
    errors = state_mod.validate_state(base_state)
    assert any("schema_version" in e for e in errors), errors


# ---------------------------------------------------------------------------
# gha_minutes_used_today (cost cap counter)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", [0, 1, 30, 1000])
def test_gha_minutes_used_today_non_negative_int(state_mod, base_state, good):
    base_state["gha_minutes_used_today"] = good
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize("bad", [-1, "0", 1.5, None, True])
def test_gha_minutes_used_today_rejects_non_int_or_negative(state_mod, base_state, bad):
    base_state["gha_minutes_used_today"] = bad
    errors = state_mod.validate_state(base_state)
    assert any("gha_minutes_used_today" in e for e in errors), errors


# ---------------------------------------------------------------------------
# gha_minutes_reset_date (ISO date, in idle_window_tz)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "good",
    ["2026-05-10", "2026-01-01", "2099-12-31"],
)
def test_reset_date_iso_format_accepted(state_mod, base_state, good):
    base_state["gha_minutes_reset_date"] = good
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize(
    "bad",
    [
        "2026/05/10",        # wrong separator
        "10-05-2026",        # wrong order
        "2026-5-10",         # unpadded
        "2026-13-01",        # invalid month
        "2026-02-30",        # invalid day
        "today",
        "",
        42,
        None,
    ],
)
def test_reset_date_malformed_rejected(state_mod, base_state, bad):
    base_state["gha_minutes_reset_date"] = bad
    errors = state_mod.validate_state(base_state)
    assert any("gha_minutes_reset_date" in e for e in errors), errors


# ---------------------------------------------------------------------------
# last_event_id (monotonic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", [0, 1, 9999])
def test_last_event_id_non_negative_int(state_mod, base_state, good):
    base_state["last_event_id"] = good
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize("bad", [-1, "0", 1.5, None, True])
def test_last_event_id_rejects_non_int_or_negative(state_mod, base_state, bad):
    base_state["last_event_id"] = bad
    errors = state_mod.validate_state(base_state)
    assert any("last_event_id" in e for e in errors), errors


# ---------------------------------------------------------------------------
# kill_switch_active (mirror of meta.kill_switch for fast-read)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v", [True, False])
def test_kill_switch_active_bool_accepted(state_mod, base_state, v):
    base_state["kill_switch_active"] = v
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "yes"])
def test_kill_switch_active_must_be_bool(state_mod, base_state, bad):
    base_state["kill_switch_active"] = bad
    errors = state_mod.validate_state(base_state)
    assert any("kill_switch_active" in e for e in errors), errors


# ---------------------------------------------------------------------------
# last_dispatch — per-routine ledger
# ---------------------------------------------------------------------------

def test_last_dispatch_must_be_dict(state_mod, base_state):
    for v in [[], "x", 42, None]:
        base_state["last_dispatch"] = v
        errors = state_mod.validate_state(base_state)
        assert any("last_dispatch" in e for e in errors), (v, errors)


def test_last_dispatch_routine_id_must_be_kebab(state_mod, base_state):
    base_state["last_dispatch"] = {
        "Bad_ID": {
            "ts": "2026-05-10T17:03:00-0700",
            "surface": "gha",
            "outcome": "ok",
        }
    }
    errors = state_mod.validate_state(base_state)
    assert any("last_dispatch" in e and "kebab" in e.lower() for e in errors), errors


@pytest.mark.parametrize(
    "missing", ["ts", "surface", "outcome"],
)
def test_dispatch_record_required_fields(state_mod, base_state, missing):
    rec = {
        "ts": "2026-05-10T17:03:00-0700",
        "surface": "gha",
        "outcome": "ok",
    }
    del rec[missing]
    base_state["last_dispatch"] = {"pr-watcher": rec}
    errors = state_mod.validate_state(base_state)
    assert any(missing in e for e in errors), errors


@pytest.mark.parametrize("surface", ["gha", "local"])
def test_dispatch_record_surface_valid(state_mod, base_state, surface):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": "2026-05-10T17:03:00-0700",
            "surface": surface,
            "outcome": "ok",
        }
    }
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize("bad", ["both", "BOTH", "cloud", "", None, 42])
def test_dispatch_record_surface_invalid(state_mod, base_state, bad):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": "2026-05-10T17:03:00-0700",
            "surface": bad,
            "outcome": "ok",
        }
    }
    errors = state_mod.validate_state(base_state)
    assert any("surface" in e for e in errors), errors


@pytest.mark.parametrize("outcome", ["ok", "noop", "warn", "err"])
def test_dispatch_record_outcome_valid(state_mod, base_state, outcome):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": "2026-05-10T17:03:00-0700",
            "surface": "gha",
            "outcome": outcome,
        }
    }
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize("bad", ["fail", "OK", "", None, 42, "skipped"])
def test_dispatch_record_outcome_invalid(state_mod, base_state, bad):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": "2026-05-10T17:03:00-0700",
            "surface": "gha",
            "outcome": bad,
        }
    }
    errors = state_mod.validate_state(base_state)
    assert any("outcome" in e for e in errors), errors


@pytest.mark.parametrize(
    "good",
    [
        "2026-05-10T17:03:00-0700",
        "2026-05-10T17:03:00+0000",
        "2026-12-31T23:59:59+0530",
    ],
)
def test_dispatch_ts_local_iso8601_accepted(state_mod, base_state, good):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": good,
            "surface": "gha",
            "outcome": "ok",
        }
    }
    assert state_mod.validate_state(base_state) == []


@pytest.mark.parametrize(
    "bad",
    [
        # PRD #10 + earlier bug report: UTC `Z` is BANNED — logs are read on
        # the user's local machine, mental UTC arithmetic is the bug.
        "2026-05-10T17:03:00Z",
        "2026-05-10",                # date only
        "17:03:00-0700",             # time only
        "2026-05-10 17:03:00-0700",  # space separator (not strict ISO)
        "now",
        "",
        42,
        None,
    ],
)
def test_dispatch_ts_malformed_or_utc_rejected(state_mod, base_state, bad):
    base_state["last_dispatch"] = {
        "pr-watcher": {
            "ts": bad,
            "surface": "gha",
            "outcome": "ok",
        }
    }
    errors = state_mod.validate_state(base_state)
    assert any("ts" in e for e in errors), errors


# ---------------------------------------------------------------------------
# initial_state helper — orchestrator/install convenience
# ---------------------------------------------------------------------------

def test_initial_state_passes_validator(state_mod):
    """The convenience constructor must produce a state dict the validator
    accepts. Otherwise install ships a corrupt state.json on day one."""
    s = state_mod.initial_state(reset_date="2026-05-10")
    assert state_mod.validate_state(s) == []
    assert s["schema_version"] == state_mod.STATE_SCHEMA_VERSION
    assert s["gha_minutes_used_today"] == 0
    assert s["last_dispatch"] == {}
    assert s["kill_switch_active"] is False
