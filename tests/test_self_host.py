"""
Self-host invariants for the auto-routines repo.

This repo eats its own dog food: `.iteration/config.yaml` installs the catalog's
archetypes onto auto-routines itself. When PRD #10 added the `meta-evolve`
archetype (priority rule 4 — re-plan iteration slices when goal.md changes),
the catalog gained the recipe but the self-host config didn't pick it up.
Until it does, every edit to .iteration/goal.md silently fails to trigger a
re-plan on this very repo.

These tests pin the missing wiring so the catalog and the self-host stay
in lockstep.
"""
from __future__ import annotations


import yaml

from .conftest import ROOT


SELF_HOST_CONFIG = ROOT / ".iteration" / "config.yaml"
RENDERER = ROOT / "scripts" / "render-routine-skills.py"
SKILLS_DIR = ROOT / ".claude" / "skills"


def _routines_by_id(config: dict) -> dict:
    return {r["id"]: r for r in config.get("routines", [])}


def test_self_host_installs_meta_evolve_routine():
    """The catalog's `meta-evolve` archetype must be installed on the
    self-host config — otherwise priority rule 4 (re-plan on goal.md
    edits) is dead code on the very repo that owns the archetype."""
    config = yaml.safe_load(SELF_HOST_CONFIG.read_text())
    routines = _routines_by_id(config)
    assert "meta-evolve" in routines, (
        "meta-evolve archetype is not installed in .iteration/config.yaml — "
        "PRD #10 priority rule 4 cannot fire on this repo. Add a routine "
        "entry mirroring commit-tests/commit-lint with primitive=git-hook "
        "and path_filters=['.iteration/goal.md']."
    )
    routine = routines["meta-evolve"]
    assert routine.get("primitive") == "git-hook", (
        "meta-evolve must be a git-hook primitive (matches the catalog "
        "archetype and the path_filters dispatch lane)"
    )
    assert routine.get("state") == "ACTIVE", (
        "meta-evolve must start ACTIVE — installing a STOPPED routine "
        "would silently drop goal.md edits on the floor"
    )
    filters = routine.get("path_filters") or []
    assert ".iteration/goal.md" in filters, (
        "meta-evolve must declare path_filters including '.iteration/goal.md' "
        "so the orchestrator's priority rule 4 short-circuits to it on the "
        "right commits"
    )


def test_renderer_has_meta_evolve_inputs_entry():
    """`scripts/render-routine-skills.py` keeps a per-routine inputs map
    (ROUTINE_SPECIFIC_INPUTS). Without an entry for meta-evolve the
    rendered SKILL.md gets the literal '(no extra inputs)' placeholder,
    which means the routine has no idea where to read goal.md or
    tasks.md from. Pin the contract so the renderer can't ship without
    it."""
    text = RENDERER.read_text()
    assert '"meta-evolve":' in text, (
        "scripts/render-routine-skills.py must list meta-evolve in "
        "ROUTINE_SPECIFIC_INPUTS — the rendered SKILL.md needs to point "
        "at .iteration/goal.md and .iteration/tasks.md as inputs"
    )
    # The block must mention both the source-of-truth file and the
    # cached task list, otherwise the routine doesn't know what to
    # diff against.
    block_start = text.find('"meta-evolve":')
    # Search a generous window: the next ~600 chars are the value tuple.
    block = text[block_start : block_start + 800]
    assert ".iteration/goal.md" in block, (
        "meta-evolve inputs must reference .iteration/goal.md — that's "
        "the file whose change triggers the routine"
    )
    assert ".iteration/tasks.md" in block, (
        "meta-evolve inputs must reference .iteration/tasks.md — that's "
        "the cached task breakdown the routine rewrites"
    )


def test_meta_evolve_skill_md_is_rendered():
    """The renderer's output for meta-evolve must already exist on disk
    — otherwise Claude Code can't load the skill when the git hook
    fires. The renderer is a one-shot, so we ship the rendered file."""
    skill_md = SKILLS_DIR / "meta-evolve" / "SKILL.md"
    assert skill_md.exists(), (
        f"{skill_md.relative_to(ROOT)} is missing — re-run "
        "`python scripts/render-routine-skills.py` and commit the output"
    )
    text = skill_md.read_text()
    # Rendered skill must carry the routine-specific inputs (not the
    # fallback '(no extra inputs)' string).
    assert "(no extra inputs)" not in text, (
        "rendered SKILL.md fell through to the '(no extra inputs)' "
        "fallback — ROUTINE_SPECIFIC_INPUTS likely lacks a meta-evolve key"
    )
    assert ".iteration/goal.md" in text, (
        "rendered SKILL.md must surface .iteration/goal.md as an input"
    )
