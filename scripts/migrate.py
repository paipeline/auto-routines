#!/usr/bin/env python3
"""
migrate.py — schema migrations for `.iteration/config.yaml`.

Called by `evolve` when it detects an existing install on an older schema
and decides to upgrade it. Each migration is a pure function: it takes a
config dict and returns a new dict, leaving the input untouched.

Public surface:
  migrate_v3_to_v4(config)       -> dict   (raises ValueError on misuse)
  migration_plan_v3_to_v4(config) -> list[str]  (preview for confirmation UI)

Defaults are picked to PRESERVE BEHAVIOR for existing installs:
  - idle_window defaults to 'always' (no idle blocking — orchestrator
    fires whenever the cron says so, just like v3 did)
  - execution_surface defaults to 'local' for scheduled/pr-poll routines
    (v3 had no GHA surface — local is what they were doing already)
  - gha_minutes_cap defaults to 60 (the cost ceiling only matters once
    the user flips a routine to 'gha'; safe upper bound until then)
  - kill_switch defaults to False (the user can flip it on later)

User-set fields are NEVER overwritten — migration is additive only.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent


def _load_sanity():
    """Import scripts/sanity-check.py despite its hyphenated filename.
    Lazy-loaded so this module has no import-time side effects."""
    spec = importlib.util.spec_from_file_location(
        "sanity_check", _HERE / "sanity-check.py"
    )
    assert spec and spec.loader, "scripts/sanity-check.py must exist"
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("sanity_check", mod)
    spec.loader.exec_module(mod)
    return mod


# Primitives that must declare execution_surface in v4. Mirrors
# SURFACE_REQUIRING_PRIMITIVES in scripts/sanity-check.py.
_SURFACE_PRIMITIVES = {"scheduled", "pr-poll"}

# Default values applied when fields are missing.
_DEFAULT_IDLE_WINDOW = "always"
_DEFAULT_GHA_CAP = 60
_DEFAULT_KILL_SWITCH = False
_DEFAULT_SURFACE = "local"
_DEFAULT_EST_MINUTES = 5


def _check_v3_input(config: Any) -> None:
    if not isinstance(config, dict):
        raise ValueError(
            f"migrate_v3_to_v4 expected a dict, got {type(config).__name__}"
        )
    sv = config.get("schema_version")
    if sv is not None and sv >= 4:
        raise ValueError(
            f"config is already at schema_version {sv}; nothing to migrate"
        )
    if sv != 3:
        raise ValueError(
            f"migrate_v3_to_v4 only accepts schema_version 3 inputs, got {sv!r}"
        )
    sanity = _load_sanity()
    errors = sanity.check(config)
    if errors:
        raise ValueError(
            f"input config is invalid as v3 — refusing to migrate: {errors}"
        )


def migrate_v3_to_v4(config: dict) -> dict:
    """Return a new dict that's a v4-shaped version of `config`.

    Raises ValueError on misuse (wrong schema version, invalid input,
    non-dict input). Does not mutate the input.
    """
    _check_v3_input(config)
    out = copy.deepcopy(config)

    meta = out.setdefault("meta", {})
    if "idle_window" not in meta:
        meta["idle_window"] = _DEFAULT_IDLE_WINDOW
    # idle_window_tz is conditional: only required when idle_window is a
    # real time range. Default 'always' doesn't need a tz, so we don't
    # invent one — let the user pick when they opt into a window.
    if "gha_minutes_cap" not in meta:
        meta["gha_minutes_cap"] = _DEFAULT_GHA_CAP
    if "kill_switch" not in meta:
        meta["kill_switch"] = _DEFAULT_KILL_SWITCH

    for r in out.get("routines", []):
        prim = r.get("primitive")
        if prim in _SURFACE_PRIMITIVES and "execution_surface" not in r:
            r["execution_surface"] = _DEFAULT_SURFACE
        if "est_minutes" not in r:
            r["est_minutes"] = _DEFAULT_EST_MINUTES

    out["schema_version"] = 4
    return out


def migration_plan_v3_to_v4(config: dict) -> list[str]:
    """Return a human-readable list of changes migrate_v3_to_v4 would make.

    Used by `evolve` to print a confirmation before rewriting config.yaml.
    Does not validate the input; callers should call migrate_v3_to_v4
    first if they want strictness."""
    if not isinstance(config, dict):
        return [f"refuse: input is not a dict ({type(config).__name__})"]

    plan: list[str] = []
    sv = config.get("schema_version")
    if sv == 4:
        return ["already at schema_version 4 — no changes"]
    if sv != 3:
        return [f"refuse: cannot migrate from schema_version {sv!r}"]

    plan.append("bump schema_version: 3 → 4")

    meta = config.get("meta", {}) or {}
    if "idle_window" not in meta:
        plan.append(f"add meta.idle_window: {_DEFAULT_IDLE_WINDOW!r}")
    if "gha_minutes_cap" not in meta:
        plan.append(f"add meta.gha_minutes_cap: {_DEFAULT_GHA_CAP}")
    if "kill_switch" not in meta:
        plan.append(f"add meta.kill_switch: {_DEFAULT_KILL_SWITCH}")

    for r in config.get("routines", []):
        rid = r.get("id", "<?>")
        prim = r.get("primitive")
        if prim in _SURFACE_PRIMITIVES and "execution_surface" not in r:
            plan.append(
                f"add routines[{rid}].execution_surface: {_DEFAULT_SURFACE!r}"
            )
        if "est_minutes" not in r:
            plan.append(
                f"add routines[{rid}].est_minutes: {_DEFAULT_EST_MINUTES}"
            )

    return plan
