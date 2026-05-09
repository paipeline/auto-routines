"""
Schema tests for templates/routine-catalog.yaml.

The catalog is what makes auto-routines "actually do work" — every archetype
ships with a prompt_body that tells the routine to write code, commit, and
open a PR. These tests are the contract: they fail-loud when an archetype
drifts back toward "just print findings."
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from .conftest import ROOT, sanity

CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"
HOOK_TEMPLATE = ROOT / "templates" / "post-commit-hook.sh"
ROUTINE_SKILL_TEMPLATE = ROOT / "templates" / "routine-skill.md"

REQUIRED_FIELDS = {
    "id", "purpose", "primitive", "trigger_default", "automation_default",
    "self_evolve", "success_criterion", "stack_hints", "prompt_body",
}


@pytest.fixture(scope="module")
def catalog() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text())


def test_catalog_loads(catalog):
    assert "archetypes" in catalog
    assert isinstance(catalog["archetypes"], list)
    assert len(catalog["archetypes"]) >= 4


def test_every_archetype_has_required_fields(catalog):
    for arch in catalog["archetypes"]:
        missing = REQUIRED_FIELDS - set(arch.keys())
        assert not missing, f"{arch.get('id', '?')} missing fields: {missing}"


def test_archetype_ids_are_kebab_and_unique(catalog):
    seen = set()
    for arch in catalog["archetypes"]:
        rid = arch["id"]
        assert sanity.KEBAB.match(rid), f"id {rid!r} not kebab-case"
        assert rid not in seen, f"duplicate archetype id: {rid}"
        seen.add(rid)


def test_archetype_primitives_valid(catalog):
    for arch in catalog["archetypes"]:
        assert arch["primitive"] in sanity.PRIMITIVES, (
            f"{arch['id']} has unknown primitive {arch['primitive']!r}"
        )


def test_archetype_automation_default_valid(catalog):
    for arch in catalog["archetypes"]:
        assert arch["automation_default"] in sanity.LEVELS


def test_archetype_self_evolve_is_bool(catalog):
    for arch in catalog["archetypes"]:
        assert isinstance(arch["self_evolve"], bool)


def test_archetype_stack_hints_is_list_of_strings(catalog):
    for arch in catalog["archetypes"]:
        assert isinstance(arch["stack_hints"], list)
        for h in arch["stack_hints"]:
            assert isinstance(h, str)


def test_archetype_prompt_body_is_substantive(catalog):
    """Prompt bodies need real substance — at least 200 chars and at least one
    imperative numbered step. Otherwise the routine ends up doing nothing."""
    for arch in catalog["archetypes"]:
        body = arch["prompt_body"]
        assert isinstance(body, str)
        assert len(body) >= 200, f"{arch['id']} prompt_body too short ({len(body)} chars)"
        assert "1." in body, f"{arch['id']} prompt_body missing numbered step 1"


# Archetypes whose "real work" is posting comments rather than branch+commit.
# They still must log and use increment_signal.
COMMENT_ONLY_ARCHETYPES = {"pr-ci-watcher"}


@pytest.mark.parametrize(
    "must_contain,applies_to_all",
    [
        ("branch", False),          # most routines branch+commit; pr-ci-watcher comments instead
        ("commit", False),          # same as above
        ("log", True),              # every routine must log to log.jsonl
        ("increment_signal", True), # every routine must mark increment for stagnation detection
    ],
)
def test_archetype_prompt_bodies_mention_real_work_idioms(catalog, must_contain, applies_to_all):
    """Every archetype's body must reference the contract that defines real
    work. If a body lacks these idioms, the routine drifts toward "analyze and
    print" — the failure mode this catalog exists to prevent. Comment-only
    archetypes (pr-ci-watcher) are exempt from branch/commit checks because
    their real work is posting PR comments."""
    for arch in catalog["archetypes"]:
        if not applies_to_all and arch["id"] in COMMENT_ONLY_ARCHETYPES:
            continue
        # Case-insensitive: "Branch:" and "branch" both count.
        assert must_contain.lower() in arch["prompt_body"].lower(), (
            f"{arch['id']} prompt_body missing {must_contain!r} — risk of "
            "drifting back to 'analyze only'"
        )


def test_expected_archetypes_are_present(catalog):
    """The four user-described routines from the bug report must exist as
    archetypes — that's the regression this whole catalog fixes."""
    ids = {arch["id"] for arch in catalog["archetypes"]}
    for required in {"commit-tests", "commit-lint", "session-test-gap", "session-doc-drift"}:
        assert required in ids, f"missing expected archetype: {required}"


# ---------------------------------------------------------------------------
# Post-commit hook template
# ---------------------------------------------------------------------------

def test_post_commit_template_exists():
    assert HOOK_TEMPLATE.exists()


def test_post_commit_template_executable():
    import os
    mode = HOOK_TEMPLATE.stat().st_mode
    assert mode & 0o100, "post-commit-hook.sh must be executable in the repo"


def test_post_commit_template_has_dispatch_placeholder():
    text = HOOK_TEMPLATE.read_text()
    assert "{{routine_dispatch_block}}" in text, (
        "post-commit-hook.sh must keep the {{routine_dispatch_block}} marker "
        "so SKILL.md install can splice routine invocations into it"
    )


def test_post_commit_template_never_blocks_commits():
    text = HOOK_TEMPLATE.read_text()
    # The hook MUST end exit 0 and trap errors so it never blocks the user's
    # commit. Catch regressions where someone removes these.
    assert "trap" in text, "hook must trap errors"
    assert "exit 0" in text, "hook must exit 0 at the end"


# ---------------------------------------------------------------------------
# Routine skill template — the per-routine SKILL.md that gets generated
# ---------------------------------------------------------------------------

def test_routine_skill_template_mandates_branch_and_pr():
    text = ROUTINE_SKILL_TEMPLATE.read_text()
    # Regression guard: the failure mode the catalog exists to fix is that
    # routines render plans instead of doing work. The template must bind
    # them to commit + push + PR.
    for required in [
        "routines/{{routine_id}}",   # branch convention
        "git push",
        "gh pr create",
        "Never push to main",
    ]:
        assert required in text, f"routine-skill.md missing: {required!r}"
