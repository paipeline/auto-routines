"""Content-contract tests for templates/routine-preamble.md.

The preamble is the deep module of PRD #10 Module 3 — single source of truth
for the FSM, log schema, automation_level dispatch, PR recipe, self-evolve
schema, and failure modes that every per-routine SKILL.md used to inline.

These tests pin the *contract* (what sections must be present, what verbatim
rules must appear) — not the prose. Prose can change; the contract can't.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PREAMBLE = REPO_ROOT / "templates" / "routine-preamble.md"


@pytest.fixture(scope="module")
def preamble_text() -> str:
    if not PREAMBLE.exists():
        pytest.fail(
            f"templates/routine-preamble.md does not exist. "
            f"PRD #10 Module 3 requires it as the single source of truth for "
            f"FSM/log-schema/PR-recipe/self-evolve/failure-modes."
        )
    return PREAMBLE.read_text()


def test_preamble_file_exists() -> None:
    """The preamble template must exist at the canonical path."""
    assert PREAMBLE.exists(), (
        "templates/routine-preamble.md must exist — it is the deep module "
        "every per-routine SKILL references."
    )


def test_preamble_is_nonempty(preamble_text: str) -> None:
    """An empty file would pass other tests trivially; require real content."""
    assert len(preamble_text.strip()) > 500, (
        "preamble looks suspiciously short — should contain 6 substantive sections"
    )


@pytest.mark.parametrize(
    "section_marker",
    [
        # FSM: state machine + transition rules
        "## State handling",
        # Output schema: log.jsonl line shape
        "## Outputs",
        # automation_level dispatch (auto / suggest / notify / off)
        "automation_level",
        # PR recipe: branch, commit, push, gh pr create
        "## You MUST commit and open a PR",
        # Self-evolve: evolve_requests.jsonl schema
        "## Self-evolution",
        # Failure modes
        "## Failure modes",
    ],
)
def test_preamble_has_canonical_section(preamble_text: str, section_marker: str) -> None:
    """Every routine reads the preamble for these six concerns. All must be present."""
    assert section_marker in preamble_text, (
        f"preamble missing canonical section marker: {section_marker!r}. "
        f"Per PRD #10 boundary contract, this section lives in the preamble."
    )


def test_preamble_has_verbatim_local_time_rule(preamble_text: str) -> None:
    """The local-time format string is pinned because past bugs (iter-001 → 001b)
    showed routines silently regressing to UTC. If this drifts, log timestamps
    become unreadable on the user's machine."""
    assert "date +%Y-%m-%dT%H:%M:%S%z" in preamble_text, (
        "preamble must contain the verbatim local-time command "
        "'date +%Y-%m-%dT%H:%M:%S%z' (NOT 'date -u' / UTC `Z`)"
    )


def test_preamble_warns_against_utc(preamble_text: str) -> None:
    """Belt-and-suspenders: explicit prohibition on UTC `Z` so the next
    template-toucher doesn't unlearn the lesson."""
    lower = preamble_text.lower()
    assert "never utc" in lower or "not `date -u`" in lower or "not utc" in lower, (
        "preamble should explicitly warn against UTC `Z` timestamps"
    )


def test_preamble_has_no_unfilled_placeholders(preamble_text: str) -> None:
    """Preamble is install-once, identical for every install — there should
    be no {{placeholders}} at all (unlike per-routine SKILLs which have many)."""
    assert "{{" not in preamble_text and "}}" not in preamble_text, (
        "preamble must contain no `{{...}}` placeholders — it's literal content "
        "shared across every install"
    )


def test_preamble_documents_all_automation_levels(preamble_text: str) -> None:
    """The dispatch table must cover all four levels — a missing level
    silently breaks routines configured at that level."""
    for level in ("auto", "suggest", "notify", "off"):
        assert f"automation_level: {level}" in preamble_text or f"`{level}`" in preamble_text, (
            f"preamble's automation_level dispatch must mention {level!r}"
        )


def test_preamble_documents_all_fsm_states(preamble_text: str) -> None:
    """FSM states are referenced by per-routine prompts ('skip if state != ACTIVE').
    All states must be documented in the preamble."""
    for state in ("ACTIVE", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED"):
        assert state in preamble_text, (
            f"preamble must document FSM state {state!r}"
        )


def test_preamble_pr_recipe_uses_force_with_lease(preamble_text: str) -> None:
    """The PR recipe should use --force-with-lease, not --force, to avoid
    clobbering concurrent pushes (e.g. user manually amending the routine branch)."""
    assert "--force-with-lease" in preamble_text, (
        "PR recipe must use --force-with-lease (not --force)"
    )


def test_preamble_pr_recipe_forbids_pushing_to_main(preamble_text: str) -> None:
    """A routine that pushes to main is a catastrophe. The rule must be explicit."""
    lower = preamble_text.lower()
    assert "never push to main" in lower or "never push to master" in lower, (
        "preamble must explicitly forbid pushing to main"
    )
