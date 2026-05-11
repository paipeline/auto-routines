"""
Schema migration tests (PRD #10 Module 5, phase 3 — story 16).

`evolve` will call migrate_v3_to_v4 on existing installs to add the new
fields with safe defaults. The contract: the migrated config must
sanity-check clean as a schema-4 config without changing any
already-set field.
"""
from __future__ import annotations

import copy
import importlib.util
import sys

import pytest

from .conftest import ROOT, sanity


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location(
        "migrate", ROOT / "scripts" / "migrate.py"
    )
    assert spec and spec.loader, "scripts/migrate.py must exist"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def migrate():
    return _load_migrate_module()


@pytest.fixture
def schema3_config() -> dict:
    """Realistic schema-3 config with two routines on different primitives."""
    return {
        "schema_version": 3,
        "repo_slug": "demo-repo",
        "goal": "ship v1",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": [
            {
                "id": "pr-watcher",
                "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "*/30 * * * *", "human": "every 30 minutes"},
                "purpose": "watch PRs",
                "automation_level": "auto",
                "self_evolve": True,
                "stagnation_threshold": 7,
            },
            {
                "id": "commit-tests",
                "state": "ACTIVE",
                "primitive": "git-hook",
                "trigger": {},
                "purpose": "regenerate tests after commits",
                "automation_level": "auto",
                "self_evolve": False,
            },
            {
                "id": "session-cleanup",
                "state": "ACTIVE",
                "primitive": "hook",
                "trigger": {"event": "Stop"},
                "purpose": "clean up after sessions",
                "automation_level": "auto",
                "self_evolve": False,
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 7,
            "default_stagnation_threshold": 7,
            "process_evolve_requests": True,
        },
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_migrate_v3_to_v4_produces_valid_v4(migrate, schema3_config):
    out = migrate.migrate_v3_to_v4(schema3_config)
    assert out["schema_version"] == 4
    # Sanity check the result against the production validator — that's
    # the contract: "migrate produces something the validator accepts".
    assert sanity.check(out) == [], sanity.check(out)


def test_migrate_is_pure(migrate, schema3_config):
    """Migration must not mutate the input — `evolve` reads the original
    config to decide whether the diff is acceptable."""
    snapshot = copy.deepcopy(schema3_config)
    migrate.migrate_v3_to_v4(schema3_config)
    assert schema3_config == snapshot, "migrate_v3_to_v4 mutated its input"


# ---------------------------------------------------------------------------
# Defaults applied — meta
# ---------------------------------------------------------------------------

def test_migrate_adds_idle_window_default_always(migrate, schema3_config):
    """Safest default — 'always' means the orchestrator never blocks on
    idle window. Users opt into a real window explicitly."""
    out = migrate.migrate_v3_to_v4(schema3_config)
    assert out["meta"]["idle_window"] == "always"
    # tz is not required when 'always' — and we don't add a wrong one
    assert "idle_window_tz" not in out["meta"]


def test_migrate_adds_gha_minutes_cap_default_60(migrate, schema3_config):
    out = migrate.migrate_v3_to_v4(schema3_config)
    assert out["meta"]["gha_minutes_cap"] == 60


def test_migrate_adds_kill_switch_default_false(migrate, schema3_config):
    out = migrate.migrate_v3_to_v4(schema3_config)
    assert out["meta"]["kill_switch"] is False


def test_migrate_respects_existing_meta_fields(migrate, schema3_config):
    """If the user already set the new fields, migrate must not stomp them."""
    schema3_config["meta"]["idle_window"] = "22:00-08:00"
    schema3_config["meta"]["idle_window_tz"] = "America/Los_Angeles"
    schema3_config["meta"]["gha_minutes_cap"] = 30
    schema3_config["meta"]["kill_switch"] = True
    out = migrate.migrate_v3_to_v4(schema3_config)
    assert out["meta"]["idle_window"] == "22:00-08:00"
    assert out["meta"]["idle_window_tz"] == "America/Los_Angeles"
    assert out["meta"]["gha_minutes_cap"] == 30
    assert out["meta"]["kill_switch"] is True


# ---------------------------------------------------------------------------
# Defaults applied — routines
# ---------------------------------------------------------------------------

def test_migrate_adds_local_surface_to_scheduled_routines(migrate, schema3_config):
    """Existing routines were all running locally (only surface available
    in v3). Default to 'local' to preserve that behavior — users explicitly
    flip routines to 'gha' as they wire up the workflow."""
    out = migrate.migrate_v3_to_v4(schema3_config)
    pr_watcher = next(r for r in out["routines"] if r["id"] == "pr-watcher")
    assert pr_watcher["execution_surface"] == "local"


def test_migrate_does_not_add_surface_to_hook_or_git_hook(migrate, schema3_config):
    """hook and git-hook routines run inside the user's session — no
    surface choice. Adding execution_surface there would be misleading."""
    out = migrate.migrate_v3_to_v4(schema3_config)
    commit_tests = next(r for r in out["routines"] if r["id"] == "commit-tests")
    assert "execution_surface" not in commit_tests
    session_cleanup = next(r for r in out["routines"] if r["id"] == "session-cleanup")
    assert "execution_surface" not in session_cleanup


def test_migrate_adds_est_minutes_default_5(migrate, schema3_config):
    """est_minutes is for the orchestrator's cost projection — every
    routine gets one (even hook routines, in case the user later promotes
    them to scheduled)."""
    out = migrate.migrate_v3_to_v4(schema3_config)
    for r in out["routines"]:
        assert r["est_minutes"] == 5, r["id"]


def test_migrate_respects_existing_routine_fields(migrate, schema3_config):
    schema3_config["routines"][0]["execution_surface"] = "gha"
    schema3_config["routines"][0]["est_minutes"] = 12
    out = migrate.migrate_v3_to_v4(schema3_config)
    pr_watcher = next(r for r in out["routines"] if r["id"] == "pr-watcher")
    assert pr_watcher["execution_surface"] == "gha"
    assert pr_watcher["est_minutes"] == 12


# ---------------------------------------------------------------------------
# Refusal cases
# ---------------------------------------------------------------------------

def test_migrate_refuses_already_v4(migrate, schema3_config):
    """Idempotency is nice in principle, but we want a loud signal if
    `evolve` accidentally re-migrates a v4 config."""
    schema3_config["schema_version"] = 4
    with pytest.raises(ValueError, match="already at schema_version 4"):
        migrate.migrate_v3_to_v4(schema3_config)


def test_migrate_refuses_pre_v3(migrate, schema3_config):
    schema3_config["schema_version"] = 2
    with pytest.raises(ValueError, match="schema_version 3"):
        migrate.migrate_v3_to_v4(schema3_config)


def test_migrate_refuses_invalid_input(migrate):
    with pytest.raises(ValueError):
        migrate.migrate_v3_to_v4("not a dict")  # type: ignore[arg-type]


def test_migrate_refuses_v3_with_validation_errors(migrate, schema3_config):
    """If the input config wouldn't pass v3 sanity-check, refuse — we
    don't silently 'fix' broken configs in the middle of a migration."""
    schema3_config["mode"] = "yolo"
    with pytest.raises(ValueError, match="invalid"):
        migrate.migrate_v3_to_v4(schema3_config)


# ---------------------------------------------------------------------------
# Migration plan — for `evolve` to show the user a diff before applying
# ---------------------------------------------------------------------------

def test_migration_plan_lists_added_fields(migrate, schema3_config):
    """Diff helper: returns a list of human-readable strings describing
    what would change. Used by `evolve` to print a confirmation before
    rewriting config.yaml."""
    plan = migrate.migration_plan_v3_to_v4(schema3_config)
    assert isinstance(plan, list)
    assert any("idle_window" in p for p in plan)
    assert any("gha_minutes_cap" in p for p in plan)
    assert any("execution_surface" in p for p in plan)
    assert any("est_minutes" in p for p in plan)


def test_migration_plan_omits_already_set_fields(migrate, schema3_config):
    schema3_config["meta"]["idle_window"] = "22:00-08:00"
    schema3_config["meta"]["idle_window_tz"] = "Europe/Berlin"
    plan = migrate.migration_plan_v3_to_v4(schema3_config)
    # Don't claim we'll add a field the user already set
    assert not any(
        "add meta.idle_window" in p.lower() and "tz" not in p.lower()
        for p in plan
    ), plan
