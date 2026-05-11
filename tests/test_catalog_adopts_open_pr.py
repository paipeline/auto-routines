"""
Drift detectors: every archetype `prompt_body` in
`templates/routine-catalog.yaml` that opens a PR must invoke the
deterministic `open-pr` wrapper (shipped earlier in this PRD), not
the raw `gh pr create` command.

Why these pins matter: open-pr was built to take the PR-assembly
step out of LLM prose — it auto-resolves `--base` from origin's
default branch, normalizes flag order, and lets tests mock the
subprocess call shape. If the catalog keeps shipping `gh pr create`
as prose, every routine fire re-derives the invocation by hand and
the wrapper rots unused — the same failure mode #58 (render-routine-
skill wire-up) and #64 (apply-fsm-plan wire-up) pinned for their
wrappers.

The affected archetypes (those whose prompt_body currently contains
`gh pr create`) are pinned individually so a removed archetype
fails loud (we did not silently drop a wire-up) rather than the
test passing vacuously.

A global regression guard forbids `gh pr create` from appearing in
ANY archetype's prompt_body. Keeping both forms is the worst case —
verbose prose wins LLM attention every time.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"


# Archetypes that previously assembled `gh pr create` by hand. If
# any of these gets renamed or removed, the per-archetype test
# below fails loud — a deliberate signal to update this list rather
# than silently dropping a wire-up.
AFFECTED_ARCHETYPE_IDS = (
    "prd-implement",
    "commit-tests",
    "commit-lint",
    "meta-evolve",
)


@pytest.fixture(scope="module")
def catalog() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def by_id(catalog) -> dict:
    return {a["id"]: a for a in catalog["archetypes"]}


# ---------------------------------------------------------------------------
# Per-archetype wire-up: prompt_body must invoke open-pr
# ---------------------------------------------------------------------------


class TestArchetypePromptBodyInvokesOpenPr:
    """Each archetype that opens a PR must invoke the wrapper. The
    pin is per-archetype (not global) so a removed archetype fails
    here rather than passing vacuously."""

    @pytest.mark.parametrize("archetype_id", AFFECTED_ARCHETYPE_IDS)
    def test_prompt_body_invokes_open_pr(self, by_id, archetype_id):
        arch = by_id.get(archetype_id)
        assert arch is not None, (
            f"archetype {archetype_id!r} is missing from catalog — "
            "if it was deliberately removed, update "
            "AFFECTED_ARCHETYPE_IDS in this test"
        )
        body = arch.get("prompt_body", "") or ""
        assert "open-pr" in body, (
            f"archetype {archetype_id!r} prompt_body must invoke "
            "`python3 scripts/orchestrator.py open-pr` — the "
            "deterministic wrapper that takes PR assembly out of "
            "LLM prose. Without this invocation, the wrapper rots "
            "unused and routines hand-assemble `gh pr create`"
        )


# ---------------------------------------------------------------------------
# Per-archetype required flags: --head, --title, --body
# ---------------------------------------------------------------------------


class TestArchetypeOpenPrRequiredFlags:
    """open-pr's argparse contract requires --head, --title, --body.
    Each affected archetype's prompt_body must reference these so
    the prose isn't ambiguous about what to pass."""

    @pytest.mark.parametrize("archetype_id", AFFECTED_ARCHETYPE_IDS)
    def test_passes_head_flag(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        idx = body.find("open-pr")
        assert idx != -1
        # The invocation can span several wrapped lines — give it a
        # generous window for the surrounding flags.
        window = body[idx:idx + 600]
        assert "--head" in window, (
            f"{archetype_id} open-pr invocation must include "
            "`--head` — open-pr's argparse requires it"
        )

    @pytest.mark.parametrize("archetype_id", AFFECTED_ARCHETYPE_IDS)
    def test_passes_title_flag(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        idx = body.find("open-pr")
        window = body[idx:idx + 600]
        assert "--title" in window, (
            f"{archetype_id} open-pr invocation must include "
            "`--title` — open-pr's argparse requires it"
        )

    @pytest.mark.parametrize("archetype_id", AFFECTED_ARCHETYPE_IDS)
    def test_passes_body_flag(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        idx = body.find("open-pr")
        window = body[idx:idx + 600]
        assert "--body" in window, (
            f"{archetype_id} open-pr invocation must include "
            "`--body` — open-pr's argparse requires it"
        )


# ---------------------------------------------------------------------------
# Global prose hygiene: `gh pr create` must NOT appear anywhere
# ---------------------------------------------------------------------------


class TestNoArchetypeAssemblesGhPrCreateByHand:
    """Regression guard: ANY archetype's prompt_body containing
    `gh pr create` invites the LLM to hand-assemble the invocation
    instead of using the wrapper. Keeping both forms is the worst
    case — verbose prose tends to win LLM attention."""

    def test_no_archetype_contains_gh_pr_create(self, catalog):
        offenders = []
        for arch in catalog["archetypes"]:
            body = arch.get("prompt_body", "") or ""
            if "gh pr create" in body:
                offenders.append(arch["id"])
        assert not offenders, (
            "These archetypes still contain `gh pr create` in their "
            f"prompt_body: {offenders}. With open-pr wired, the raw "
            "`gh pr create` reference must be removed — keeping both "
            "invites the LLM to drift back to hand-assembly"
        )


# ---------------------------------------------------------------------------
# Pinned invocation prefix: must use orchestrator.py
# ---------------------------------------------------------------------------


class TestOpenPrInvokedViaOrchestrator:
    """The wrapper is `python3 scripts/orchestrator.py open-pr`. The
    pin: each affected archetype's invocation must go through
    orchestrator.py, not call a bare `open-pr` binary (which doesn't
    exist) or some other entry point."""

    @pytest.mark.parametrize("archetype_id", AFFECTED_ARCHETYPE_IDS)
    def test_open_pr_called_via_orchestrator(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        # The literal `scripts/orchestrator.py open-pr` substring
        # pins the canonical invocation prefix.
        assert re.search(r"scripts/orchestrator\.py\s+open-pr", body), (
            f"{archetype_id} open-pr invocation must be prefixed by "
            "`scripts/orchestrator.py open-pr` — the canonical entry "
            "point. A bare `open-pr` reference would fail at exec"
        )
