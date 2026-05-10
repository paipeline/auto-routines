"""
TDD harness for scripts/sanity-check.py.

Every guardrail in SKILL.md should fail-loud here when violated. Add a test
*before* you add a check — that's the contract for sanity-check changes.
"""
from __future__ import annotations

import copy

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
    """Minimal valid schema-2 config used as a starting point for negative tests."""
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


@pytest.fixture
def schema3_config() -> dict:
    """Minimal valid schema-3 config — FSM state, human-readable schedule, etc."""
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
            }
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


@pytest.fixture
def schema4_config() -> dict:
    """Minimal valid schema-4 config — adds idle window, GHA cost cap,
    per-routine execution_surface + est_minutes, kill switch."""
    return {
        "schema_version": 4,
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
                "execution_surface": "gha",
                "est_minutes": 4,
            }
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 7,
            "default_stagnation_threshold": 7,
            "process_evolve_requests": True,
            "idle_window": "22:00-08:00",
            "idle_window_tz": "America/Los_Angeles",
            "gha_minutes_cap": 60,
            "kill_switch": False,
        },
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


# ---------------------------------------------------------------------------
# Schema 3 — finite state machine + human-readable triggers + self-evolve
# ---------------------------------------------------------------------------

def test_schema3_template_passes(schema3_config):
    assert sanity.check(schema3_config) == []


def test_schema3_state_required(schema3_config):
    del schema3_config["routines"][0]["state"]
    errors = sanity.check(schema3_config)
    assert any("state" in e for e in errors), errors


def test_schema2_state_optional(base_config):
    # Backward compat: schema 2 doesn't require state
    assert sanity.check(base_config) == []
    base_config["routines"][0]["state"] = "ACTIVE"
    assert sanity.check(base_config) == []


@pytest.mark.parametrize(
    "state",
    ["PROPOSED", "ACTIVE", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED"],
)
def test_all_fsm_states_accepted(schema3_config, state):
    schema3_config["routines"][0]["state"] = state
    assert sanity.check(schema3_config) == []


@pytest.mark.parametrize("bogus", ["RUNNING", "active", "Done", "", "unknown"])
def test_invalid_state_rejected(schema3_config, bogus):
    schema3_config["routines"][0]["state"] = bogus
    errors = sanity.check(schema3_config)
    assert any("state" in e for e in errors), errors


def test_schema3_trigger_human_required_with_cron(schema3_config):
    del schema3_config["routines"][0]["trigger"]["human"]
    errors = sanity.check(schema3_config)
    assert any("trigger.human" in e for e in errors), errors


def test_trigger_human_must_be_string(schema3_config):
    schema3_config["routines"][0]["trigger"]["human"] = 42
    errors = sanity.check(schema3_config)
    assert any("trigger.human" in e for e in errors)


def test_schema3_meta_human_required(schema3_config):
    del schema3_config["meta"]["human"]
    errors = sanity.check(schema3_config)
    assert any("meta.human" in e for e in errors), errors


def test_self_evolve_must_be_bool(schema3_config):
    schema3_config["routines"][0]["self_evolve"] = "yes"
    errors = sanity.check(schema3_config)
    assert any("self_evolve" in e for e in errors)


@pytest.mark.parametrize("bad", [0, -1, "seven", 1.5, None])
def test_stagnation_threshold_must_be_positive_int(schema3_config, bad):
    schema3_config["routines"][0]["stagnation_threshold"] = bad
    errors = sanity.check(schema3_config)
    assert any("stagnation_threshold" in e for e in errors), errors


def test_meta_default_stagnation_threshold_must_be_positive_int(schema3_config):
    schema3_config["meta"]["default_stagnation_threshold"] = 0
    errors = sanity.check(schema3_config)
    assert any("default_stagnation_threshold" in e for e in errors)


def test_meta_process_evolve_requests_must_be_bool(schema3_config):
    schema3_config["meta"]["process_evolve_requests"] = "true"
    errors = sanity.check(schema3_config)
    assert any("process_evolve_requests" in e for e in errors)


# ---------------------------------------------------------------------------
# FSM helpers exposed by sanity module (used by SKILL.md / status command)
# ---------------------------------------------------------------------------

def test_state_set_constants_exposed():
    assert sanity.ROUTINE_STATES == {
        "PROPOSED", "ACTIVE", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED",
    }
    assert sanity.FIRING_STATES == {"ACTIVE", "EVOLVING"}
    assert sanity.PAUSED_STATES == {"STAGNANT", "COMPLETED"}
    assert sanity.TERMINAL_STATES == {"STOPPED"}


# ---------------------------------------------------------------------------
# meta.budget — controls the cadence presets in SKILL.md
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["low", "medium", "high", "custom"])
def test_meta_budget_accepts_known_tiers(schema3_config, tier):
    schema3_config["meta"]["budget"] = tier
    assert sanity.check(schema3_config) == []


@pytest.mark.parametrize("bad", ["medium-high", "free", "", 5, None, True])
def test_meta_budget_rejects_unknown_values(schema3_config, bad):
    schema3_config["meta"]["budget"] = bad
    errors = sanity.check(schema3_config)
    assert any("meta.budget" in e for e in errors), errors


def test_meta_budget_optional(schema3_config):
    """Configs without meta.budget still validate — keeps schema-2 configs working."""
    schema3_config["meta"].pop("budget", None)
    assert sanity.check(schema3_config) == []


def test_meta_budget_constants_exposed():
    """SKILL.md and status.py reference these tiers — pin them."""
    assert sanity.BUDGET_TIERS == {"low", "medium", "high", "custom"}


# ---------------------------------------------------------------------------
# Schema 4 — execution_surface, idle_window, GHA cost cap, kill switch
# (PRD #10 Module 5 — adaptive responsiveness + cost ceiling for GHA)
# ---------------------------------------------------------------------------

def test_schema4_template_passes(schema4_config):
    assert sanity.check(schema4_config) == []


# ---- meta.idle_window -----------------------------------------------------

def test_schema4_idle_window_required(schema4_config):
    """Schema 4 mandates an idle window (or 'always') so the orchestrator
    knows when it's allowed to dispatch GHA work."""
    del schema4_config["meta"]["idle_window"]
    errors = sanity.check(schema4_config)
    assert any("idle_window" in e for e in errors), errors


@pytest.mark.parametrize(
    "window",
    [
        "22:00-08:00",   # overnight (wraps midnight)
        "09:00-17:00",   # daytime
        "00:00-23:59",   # all but last minute
        "always",        # never idle — work any time
    ],
)
def test_idle_window_valid_forms_accepted(schema4_config, window):
    schema4_config["meta"]["idle_window"] = window
    if window == "always":
        # tz becomes optional when 'always'
        schema4_config["meta"].pop("idle_window_tz", None)
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize(
    "bad",
    [
        "22-08",          # missing minutes
        "22:00",          # missing range
        "22:00-",         # missing end
        "22:00:00-08:00", # seconds not allowed
        "25:00-08:00",    # hour out of range
        "22:60-08:00",    # minute out of range
        "always 22:00",   # garbage
        "",
        42,
        None,
    ],
)
def test_idle_window_malformed_rejected(schema4_config, bad):
    schema4_config["meta"]["idle_window"] = bad
    errors = sanity.check(schema4_config)
    assert any("idle_window" in e for e in errors), errors


# ---- meta.idle_window_tz --------------------------------------------------

def test_idle_window_tz_required_when_window_set(schema4_config):
    """If idle_window is a real time range, idle_window_tz is mandatory.
    Skipping it would silently default to UTC and surprise the user."""
    del schema4_config["meta"]["idle_window_tz"]
    # idle_window is "22:00-08:00" — definitely needs a tz
    errors = sanity.check(schema4_config)
    assert any("idle_window_tz" in e for e in errors), errors


def test_idle_window_tz_optional_when_always(schema4_config):
    """'always' means no idle window, so tz is moot."""
    schema4_config["meta"]["idle_window"] = "always"
    schema4_config["meta"].pop("idle_window_tz", None)
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize(
    "tz",
    [
        "America/Los_Angeles",
        "Europe/Berlin",
        "Asia/Tokyo",
        "UTC",
    ],
)
def test_idle_window_tz_iana_accepted(schema4_config, tz):
    schema4_config["meta"]["idle_window_tz"] = tz
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize(
    "bad",
    ["PST", "GMT+8", "Mars/Olympus", "", 42, None],
)
def test_idle_window_tz_non_iana_rejected(schema4_config, bad):
    schema4_config["meta"]["idle_window_tz"] = bad
    errors = sanity.check(schema4_config)
    assert any("idle_window_tz" in e for e in errors), errors


# ---- meta.gha_minutes_cap -------------------------------------------------

def test_gha_minutes_cap_optional(schema4_config):
    """Default is 60 (matches PRD #10 — story 30); validator just checks
    the field is well-formed when present."""
    schema4_config["meta"].pop("gha_minutes_cap", None)
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("cap", [1, 30, 60, 240, 1440])
def test_gha_minutes_cap_positive_int_accepted(schema4_config, cap):
    schema4_config["meta"]["gha_minutes_cap"] = cap
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("bad", [0, -1, "60", 1.5, None, True])
def test_gha_minutes_cap_must_be_positive_int(schema4_config, bad):
    schema4_config["meta"]["gha_minutes_cap"] = bad
    errors = sanity.check(schema4_config)
    assert any("gha_minutes_cap" in e for e in errors), errors


# ---- meta.kill_switch (story 29) ------------------------------------------

def test_kill_switch_optional(schema4_config):
    schema4_config["meta"].pop("kill_switch", None)
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("v", [True, False])
def test_kill_switch_bool_accepted(schema4_config, v):
    schema4_config["meta"]["kill_switch"] = v
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, "true"])
def test_kill_switch_must_be_bool(schema4_config, bad):
    schema4_config["meta"]["kill_switch"] = bad
    errors = sanity.check(schema4_config)
    assert any("kill_switch" in e for e in errors), errors


# ---- routines[].execution_surface -----------------------------------------

@pytest.mark.parametrize("surface", ["gha", "local"])
def test_execution_surface_valid_values_accepted(schema4_config, surface):
    schema4_config["routines"][0]["execution_surface"] = surface
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("bad", ["both", "BOTH", "cloud", "", None, "GHA"])
def test_execution_surface_rejects_invalid(schema4_config, bad):
    """No 'both' — that was the reviewer-flagged ambiguity in PRD #10
    (each routine fires on exactly one surface)."""
    schema4_config["routines"][0]["execution_surface"] = bad
    errors = sanity.check(schema4_config)
    assert any("execution_surface" in e for e in errors), errors


def test_execution_surface_required_for_scheduled_at_schema_4(schema4_config):
    """Scheduled and pr-poll routines must declare which surface they run on
    — that's how the orchestrator routes dispatch."""
    del schema4_config["routines"][0]["execution_surface"]
    errors = sanity.check(schema4_config)
    assert any("execution_surface" in e for e in errors), errors


def test_execution_surface_required_for_pr_poll_at_schema_4(schema4_config):
    schema4_config["routines"][0]["primitive"] = "pr-poll"
    del schema4_config["routines"][0]["execution_surface"]
    errors = sanity.check(schema4_config)
    assert any("execution_surface" in e for e in errors), errors


def test_execution_surface_not_required_for_hook(schema4_config):
    """Hook routines run inside the user's Claude session — no surface choice."""
    schema4_config["routines"][0]["primitive"] = "hook"
    schema4_config["routines"][0]["trigger"] = {"event": "Stop"}
    schema4_config["routines"][0].pop("execution_surface", None)
    assert sanity.check(schema4_config) == []


def test_execution_surface_not_required_for_git_hook(schema4_config):
    schema4_config["routines"][0]["primitive"] = "git-hook"
    schema4_config["routines"][0]["trigger"] = {}
    schema4_config["routines"][0].pop("execution_surface", None)
    assert sanity.check(schema4_config) == []


def test_execution_surface_optional_at_schema_3(schema3_config):
    """Backward compat: schema 3 configs (no execution_surface anywhere)
    must keep validating after the schema 4 rule lands."""
    assert sanity.check(schema3_config) == []


# ---- routines[].est_minutes -----------------------------------------------

def test_est_minutes_optional(schema4_config):
    """Default is 5 minutes per fire (used by orchestrator to project
    cost against gha_minutes_cap)."""
    schema4_config["routines"][0].pop("est_minutes", None)
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("est", [1, 4, 10, 60])
def test_est_minutes_positive_int_accepted(schema4_config, est):
    schema4_config["routines"][0]["est_minutes"] = est
    assert sanity.check(schema4_config) == []


@pytest.mark.parametrize("bad", [0, -1, "5", 4.5, None, True])
def test_est_minutes_must_be_positive_int(schema4_config, bad):
    schema4_config["routines"][0]["est_minutes"] = bad
    errors = sanity.check(schema4_config)
    assert any("est_minutes" in e for e in errors), errors


# ---- schema_version bump --------------------------------------------------

def test_schema_version_4_accepted(schema4_config):
    """Sanity: the validator recognizes schema_version 4 and applies the new rules."""
    assert sanity.check(schema4_config) == []
    # And drops back into schema 3 mode when the version is older
    schema4_config["schema_version"] = 3
    schema4_config["meta"].pop("idle_window", None)
    schema4_config["meta"].pop("idle_window_tz", None)
    schema4_config["routines"][0].pop("execution_surface", None)
    assert sanity.check(schema4_config) == []
