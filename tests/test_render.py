"""Tests for the per-routine SKILL.md renderer (PRD #10 Module 3, Phase 2 + 3).

These tests pin the *output contract* of the renderer:

- The slim per-routine template drops boilerplate sections that moved to
  `_shared/preamble.md` (FSM, Outputs, Self-evolution, Failure modes, PR
  recipe).
- It keeps the routine-specific sections (Purpose, Trigger, prompt body,
  Inputs).
- It contains a `## Reference` pointer to the preamble.
- The rendered file is ≤ 3000 bytes (token-frugality rule from PRD #10).
- The pre-existing double-bullet bug (`- - ...` on line 20 of every
  rendered SKILL today) is fixed.

The renderer is exercised as a *pure function* `render_routine_skill(...)`.
That function takes its inputs explicitly so tests don't depend on any
particular `.iteration/config.yaml` existing on disk.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDERER_PATH = REPO_ROOT / "scripts" / "render-routine-skills.py"
TEMPLATE_PATH = REPO_ROOT / "templates" / "routine-skill.md"
CATALOG_PATH = REPO_ROOT / "templates" / "routine-catalog.yaml"


def _load_renderer():
    """Import scripts/render-routine-skills.py despite the hyphen in the name."""
    spec = importlib.util.spec_from_file_location("renderer", RENDERER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["renderer"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def renderer():
    return _load_renderer()


@pytest.fixture(scope="module")
def archetypes() -> dict:
    catalog = yaml.safe_load(CATALOG_PATH.read_text())
    return {a["id"]: a for a in catalog["archetypes"]}


@pytest.fixture
def sample_routine() -> dict:
    """A representative routine config — what would appear in
    .iteration/config.yaml under `routines:`."""
    return {
        "id": "commit-tests",
        "purpose": "Run tests after every commit and add coverage for new code paths.",
        "primitive": "scheduled",
        "iter_added": 1,
        "automation_level": "auto",
        "self_evolve": True,
        "state": "ACTIVE",
        "trigger": {"human": "after every commit", "cron": "*/15 * * * *"},
        "success_criterion": "every commit on main has green tests within 5 minutes",
    }


# ---------------------------------------------------------------------------
# Pure-function contract: render_routine_skill(...)
# ---------------------------------------------------------------------------

def test_renderer_exposes_pure_render_function(renderer) -> None:
    """The renderer must expose a pure function for testing — not be a
    script that only runs main()."""
    assert hasattr(renderer, "render_routine_skill"), (
        "renderer must expose render_routine_skill(template_text, routine, "
        "archetype, installed_at) as a pure function"
    )


def test_render_returns_str(renderer, sample_routine, archetypes) -> None:
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    assert isinstance(out, str) and len(out) > 0


def test_render_has_no_unfilled_placeholders(
    renderer, sample_routine, archetypes
) -> None:
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    assert "{{" not in out and "}}" not in out, (
        f"unfilled placeholders in rendered SKILL:\n{out}"
    )


# ---------------------------------------------------------------------------
# Slim-template contract: what the rendered SKILL must / must not contain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "section_marker",
    [
        "## Purpose",
        "## Trigger",
        "## Success criterion",
        "## Inputs to read at fire time",
        "## What to do",
    ],
)
def test_rendered_skill_keeps_routine_specific_section(
    renderer, sample_routine, archetypes, section_marker
) -> None:
    """Per PRD #10 boundary contract — these sections live in the per-routine
    file because they vary per routine."""
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    assert section_marker in out, (
        f"slim per-routine SKILL must keep {section_marker!r}"
    )


@pytest.mark.parametrize(
    "moved_section",
    [
        "## State handling",
        "## Failure modes",
        "## Self-evolution",
        "## Outputs",
        "## You MUST commit and open a PR",
    ],
)
def test_rendered_skill_drops_moved_section(
    renderer, sample_routine, archetypes, moved_section
) -> None:
    """Per PRD #10 boundary contract — these sections moved to
    `_shared/preamble.md`. Their presence in the per-routine SKILL means
    the template wasn't slimmed."""
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    assert moved_section not in out, (
        f"slim per-routine SKILL must NOT contain {moved_section!r} "
        f"— it lives in _shared/preamble.md now"
    )


def test_rendered_skill_has_reference_pointer(
    renderer, sample_routine, archetypes
) -> None:
    """The slim per-routine SKILL must point at the preamble so the routine
    knows where to look when its prompt body asks about FSM / output / PR
    mechanics."""
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    assert "_shared/preamble.md" in out, (
        "rendered SKILL must reference `_shared/preamble.md` so the routine "
        "knows where to read FSM/output/PR/self-evolve/failure-modes from"
    )


def test_rendered_skill_has_no_double_bullet(
    renderer, sample_routine, archetypes
) -> None:
    """Pre-existing bug at templates/routine-skill.md:20 — the line
    `- {{routine_specific_inputs}}` collides with bullet-prefixed values
    in ROUTINE_SPECIFIC_INPUTS, producing `- - ...` in rendered SKILLs.
    PRD #10 explicitly absorbs this fix."""
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    assert "\n- - " not in out, (
        f"double-bullet rendering bug present:\n{out}"
    )


# ---------------------------------------------------------------------------
# Byte-budget rule
# ---------------------------------------------------------------------------

DEFAULT_BYTE_BUDGET = 3000


@pytest.mark.parametrize(
    "archetype_id",
    [
        # The simple-execution routines from the catalog. Coordinator-style
        # archetypes with extensive decision trees may exceed the default
        # via per-routine override; that's tested in test_sanity_check.py.
        "commit-tests",
        "commit-lint",
        "daily-digest",
        "session-doc-drift",
    ],
)
def test_rendered_skill_under_byte_budget(
    renderer, archetypes, archetype_id
) -> None:
    """Every rendered per-routine SKILL must be ≤ 3KB at the default budget.
    This is the load-bearing token-frugality rule from PRD #10."""
    arch = archetypes[archetype_id]
    routine = {
        "id": archetype_id,
        "purpose": arch["purpose"],
        "primitive": arch["primitive"],
        "iter_added": 1,
        "automation_level": arch.get("automation_default", "auto"),
        "self_evolve": arch.get("self_evolve", False),
        "state": "ACTIVE",
        "trigger": {"human": arch["trigger_default"], "cron": "0 * * * *"},
        "success_criterion": arch.get("success_criterion") or None,
    }
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=routine,
        archetype=arch,
        installed_at="2026-05-10T10:00:00+0200",
    )
    size = len(out.encode("utf-8"))
    assert size <= DEFAULT_BYTE_BUDGET, (
        f"rendered SKILL for {archetype_id!r} is {size} bytes "
        f"> {DEFAULT_BYTE_BUDGET} budget"
    )


def test_rendered_skill_substantively_smaller_than_old(
    renderer, sample_routine, archetypes
) -> None:
    """Sanity check that the slim template is meaningfully smaller —
    not just trimmed by 50 bytes. Previous size was ~8.5KB; new must
    be at least 60% smaller."""
    out = renderer.render_routine_skill(
        template_text=TEMPLATE_PATH.read_text(),
        routine=sample_routine,
        archetype=archetypes["commit-tests"],
        installed_at="2026-05-10T10:00:00+0200",
    )
    size = len(out.encode("utf-8"))
    OLD_BASELINE = 8500  # bytes — pre-PRD #10 typical rendered size
    assert size < OLD_BASELINE * 0.40, (
        f"rendered SKILL is {size} bytes — expected substantial reduction "
        f"from old baseline ~{OLD_BASELINE} bytes (target < 40%)"
    )
