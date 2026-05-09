"""
TDD harness for scripts/sanity-check.py.

Every guardrail in SKILL.md should fail-loud here when violated. Add a test
*before* you add a check — that's the contract for sanity-check changes.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from .conftest import ROOT, sanity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def template_config() -> dict:
    """The shipped template — must pass sanity check unmodified."""
    text = (ROOT / "templates" / "config.yaml").read_text()
    return yaml.safe_load(text)


@pytest.fixture
def base_config() -> dict:
    """Minimal valid config used as a starting point for negative tests."""
    return {
        "schema_version": 2,
        "repo_slug": "demo-repo",
        "goal": "ship v1",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": [
            {
                "id": "pr-watcher",
                "primitive": "scheduled",
                "trigger": {"cron": "*/30 * * * *"},
                "purpose": "watch PRs",
                "automation_level": "auto",
            }
        ],
        "neutralized_tasks": [],
        "meta": {"cron": "0 9 * * *", "anti_flap_window": 7},
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_template_passes(template_config):
    assert sanity.check(template_config) == []


def test_minimal_valid_config_passes(base_config):
    assert sanity.check(base_config) == []


@pytest.mark.parametrize("mode", ["goal-driven", "fully-auto"])
def test_both_modes_accepted(base_config, mode):
    base_config["mode"] = mode
    assert sanity.check(base_config) == []


# ---------------------------------------------------------------------------
# Required keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing",
    ["schema_version", "repo_slug", "goal", "mode", "deps", "routines", "meta"],
)
def test_missing_top_level_key_fails(base_config, missing):
    del base_config[missing]
    errors = sanity.check(base_config)
    assert any(missing in e for e in errors), errors


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

def test_invalid_mode_fails(base_config):
    base_config["mode"] = "yolo"
    errors = sanity.check(base_config)
    assert any("mode" in e for e in errors)


# ---------------------------------------------------------------------------
# repo_slug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slug",
    [
        "Demo_Repo",     # underscore + uppercase
        "-leading",      # leading dash
        "trailing-",     # trailing dash
        "double--dash",  # double dash
        "",              # empty
        "x" * 33,        # over length cap
    ],
)
def test_bad_slug_fails(base_config, slug):
    base_config["repo_slug"] = slug
    errors = sanity.check(base_config)
    assert any("repo_slug" in e for e in errors), errors


def test_taskid_length_overflow_caught(base_config):
    base_config["repo_slug"] = "x" * 32
    base_config["routines"][0]["id"] = "y" * 80
    errors = sanity.check(base_config)
    assert any("taskId" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------

def test_duplicate_routine_id_fails(base_config):
    base_config["routines"].append(copy.deepcopy(base_config["routines"][0]))
    errors = sanity.check(base_config)
    assert any("duplicate" in e.lower() for e in errors)


def test_non_kebab_routine_id_fails(base_config):
    base_config["routines"][0]["id"] = "PR_Watcher"
    errors = sanity.check(base_config)
    assert any("kebab" in e.lower() for e in errors)


@pytest.mark.parametrize("reserved", ["meta", "__meta__"])
def test_reserved_meta_id_blocked(base_config, reserved):
    base_config["routines"][0]["id"] = reserved
    errors = sanity.check(base_config)
    assert any("reserved" in e.lower() for e in errors)


def test_invalid_primitive_fails(base_config):
    base_config["routines"][0]["primitive"] = "magic"
    errors = sanity.check(base_config)
    assert any("primitive" in e for e in errors)


def test_invalid_automation_level_fails(base_config):
    base_config["routines"][0]["automation_level"] = "yes-please"
    errors = sanity.check(base_config)
    assert any("automation_level" in e for e in errors)


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def test_scheduled_routine_requires_cron(base_config):
    del base_config["routines"][0]["trigger"]["cron"]
    errors = sanity.check(base_config)
    assert any("cron" in e for e in errors)


@pytest.mark.parametrize(
    "cron",
    [
        "* * * *",          # 4 fields
        "* * * * * *",      # 6 fields
        "60 * * * *",       # minute out of range
        "* 25 * * *",       # hour out of range
        "* * 0 * *",        # day-of-month < 1
        "* * * 13 *",       # month > 12
        "* * * * 8/",       # malformed step
        "abc",              # garbage
    ],
)
def test_invalid_cron_fails(base_config, cron):
    base_config["routines"][0]["trigger"]["cron"] = cron
    errors = sanity.check(base_config)
    assert errors, f"expected failure for cron={cron!r}"


@pytest.mark.parametrize(
    "cron",
    [
        "*/15 * * * *",
        "0 9 * * *",
        "0 17 * * 1-5",
        "0,30 * * * *",
        "0 9-17/2 * * 1-5",
    ],
)
def test_valid_cron_accepted(base_config, cron):
    base_config["routines"][0]["trigger"]["cron"] = cron
    assert sanity.check(base_config) == []


def test_hook_primitive_requires_known_event(base_config):
    base_config["routines"][0]["primitive"] = "hook"
    base_config["routines"][0]["trigger"] = {"event": "PostCommit"}
    errors = sanity.check(base_config)
    assert any("event" in e for e in errors)


def test_hook_primitive_accepts_real_event(base_config):
    base_config["routines"][0]["primitive"] = "hook"
    base_config["routines"][0]["trigger"] = {"event": "Stop"}
    assert sanity.check(base_config) == []


def test_git_hook_primitive_accepted(base_config):
    base_config["routines"][0]["primitive"] = "git-hook"
    base_config["routines"][0]["trigger"] = {}
    assert sanity.check(base_config) == []


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

def test_meta_cron_required(base_config):
    del base_config["meta"]["cron"]
    errors = sanity.check(base_config)
    assert any("meta.cron" in e for e in errors)


def test_meta_anti_flap_window_must_be_int(base_config):
    base_config["meta"]["anti_flap_window"] = "seven"
    errors = sanity.check(base_config)
    assert any("anti_flap_window" in e for e in errors)


# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------

def test_invalid_gh_value_fails(base_config):
    base_config["deps"]["gh"] = "maybe"
    errors = sanity.check(base_config)
    assert any("deps.gh" in e for e in errors)


def test_mcps_must_be_list_of_strings(base_config):
    base_config["deps"]["mcps"] = [123]
    errors = sanity.check(base_config)
    assert any("deps.mcps" in e for e in errors)


# ---------------------------------------------------------------------------
# Neutralized tasks
# ---------------------------------------------------------------------------

def test_neutralized_must_be_list(base_config):
    base_config["neutralized_tasks"] = {"oops": True}
    errors = sanity.check(base_config)
    assert any("neutralized_tasks" in e for e in errors)


def test_neutralized_entry_requires_keys(base_config):
    base_config["neutralized_tasks"] = [{"task_id": "x"}]
    errors = sanity.check(base_config)
    assert any("missing key" in e for e in errors)


def test_neutralized_taskid_cannot_alias_active_routine(base_config):
    base_config["routines"][0]["task_id"] = "auto-routines-demo-repo-pr-watcher"
    base_config["routines"][0]["enabled"] = True
    base_config["neutralized_tasks"] = [
        {
            "task_id": "auto-routines-demo-repo-pr-watcher",
            "original_routine_id": "pr-watcher",
            "neutralized_at_iter": 5,
        }
    ]
    errors = sanity.check(base_config)
    assert any("neutralized" in e.lower() and "active" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Cron-field helper (unit-level)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,lo,hi,expected",
    [
        ("*", 0, 59, True),
        ("*/15", 0, 59, True),
        ("0,30", 0, 59, True),
        ("9-17", 0, 23, True),
        ("60", 0, 59, False),
        ("9-17/2", 0, 23, True),
        ("abc", 0, 59, False),
    ],
)
def test_cron_field_ok(field, lo, hi, expected):
    assert sanity.cron_field_ok(field, lo, hi) is expected
