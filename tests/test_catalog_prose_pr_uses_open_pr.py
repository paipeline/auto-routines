"""
Drift detectors: archetypes that DESCRIBE PR-opening in prose but
never invoke the deterministic `open-pr` wrapper.

Tick 43 (PR #66) wired open-pr into the 4 archetypes that previously
hand-assembled `gh pr create`. This slice catches the next mode of
drift: archetypes whose `prompt_body` says "open a PR" or "open PR
titled X" without specifying HOW, leaving the LLM to re-derive the
invocation every fire. Same wrapper-rot failure mode (#58, #64, #66
all pinned the same anti-pattern for their respective wrappers).

The affected archetypes here are the ones whose prompt_body
mentioned opening a PR in prose, did NOT contain `gh pr create`
verbatim, and did NOT previously invoke `open-pr`:

  - daily-digest          (budget-check path + LLM path, both open PRs)
  - weekly-dep-audit      (advisory-bump PR)
  - coverage-watcher      (fixable + tracking PR scenarios)
  - release-tag-checker   (missed-version-bump PR)

(Excluded: pr-ci-watcher, pr-review-bot, secret-scan — these post
COMMENTS on existing PRs via `gh pr comment` / `gh pr review`,
they don't open new PRs. open-pr doesn't apply.)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"


# Archetypes whose prompt_body describes PR-opening in prose and
# must now invoke the wrapper. Pinned individually so a renamed or
# removed archetype fails loud (don't silently drop a wire-up).
PROSE_PR_ARCHETYPE_IDS = (
    "daily-digest",
    "weekly-dep-audit",
    "coverage-watcher",
    "release-tag-checker",
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


class TestProsePrArchetypeInvokesOpenPr:
    """The wire-up: each of these archetypes must invoke
    `python3 scripts/orchestrator.py open-pr` directly. Saying
    "open a PR" in prose without specifying the invocation lets
    the LLM re-derive the call shape every fire — exactly the
    drift mode the wrapper was built to prevent."""

    @pytest.mark.parametrize("archetype_id", PROSE_PR_ARCHETYPE_IDS)
    def test_prompt_body_invokes_open_pr(self, by_id, archetype_id):
        arch = by_id.get(archetype_id)
        assert arch is not None, (
            f"archetype {archetype_id!r} is missing from catalog — "
            "if it was deliberately removed, update "
            "PROSE_PR_ARCHETYPE_IDS in this test"
        )
        body = arch.get("prompt_body", "") or ""
        assert "open-pr" in body, (
            f"archetype {archetype_id!r} prompt_body describes "
            "opening a PR but never invokes the deterministic "
            "`open-pr` wrapper. Replace 'open a PR' prose with "
            "`python3 scripts/orchestrator.py open-pr --head "
            "<branch> --title <...> --body <...>` so PR assembly "
            "leaves LLM prose"
        )


# ---------------------------------------------------------------------------
# Per-archetype required flags: --head, --title, --body
# ---------------------------------------------------------------------------


class TestProsePrArchetypeOpenPrRequiredFlags:
    """open-pr's argparse contract requires --head, --title, --body.
    Each affected archetype's invocation must surface these so the
    prose isn't ambiguous about what to pass."""

    @pytest.mark.parametrize("archetype_id", PROSE_PR_ARCHETYPE_IDS)
    def test_passes_head_flag(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        idx = body.find("open-pr")
        assert idx != -1
        window = body[idx:idx + 700]
        assert "--head" in window, (
            f"{archetype_id} open-pr invocation must include "
            "`--head` — open-pr's argparse requires it"
        )

    @pytest.mark.parametrize("archetype_id", PROSE_PR_ARCHETYPE_IDS)
    def test_passes_title_flag(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        idx = body.find("open-pr")
        window = body[idx:idx + 700]
        assert "--title" in window, (
            f"{archetype_id} open-pr invocation must include "
            "`--title` — open-pr's argparse requires it"
        )

    @pytest.mark.parametrize("archetype_id", PROSE_PR_ARCHETYPE_IDS)
    def test_passes_body_flag(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        idx = body.find("open-pr")
        window = body[idx:idx + 700]
        assert "--body" in window, (
            f"{archetype_id} open-pr invocation must include "
            "`--body` — open-pr's argparse requires it"
        )


# ---------------------------------------------------------------------------
# Pinned invocation prefix: must use orchestrator.py
# ---------------------------------------------------------------------------


class TestProsePrArchetypeUsesOrchestrator:
    """Each invocation must be prefixed by `scripts/orchestrator.py
    open-pr` — bare `open-pr` would fail at exec (no such binary)."""

    @pytest.mark.parametrize("archetype_id", PROSE_PR_ARCHETYPE_IDS)
    def test_invoked_via_orchestrator(self, by_id, archetype_id):
        body = by_id[archetype_id].get("prompt_body", "") or ""
        assert re.search(r"scripts/orchestrator\.py\s+open-pr", body), (
            f"{archetype_id} open-pr invocation must be prefixed "
            "by `scripts/orchestrator.py open-pr` — the canonical "
            "entry point"
        )


# ---------------------------------------------------------------------------
# Coverage-watcher: BOTH PR scenarios (fixable + tracking) must wire up
# ---------------------------------------------------------------------------


class TestCoverageWatcherBothScenariosWired:
    """coverage-watcher has two distinct PR-opening branches: the
    'fixable in one fire' restore PR and the 'tracking PR' for
    larger gaps. Both must invoke open-pr — a single invocation
    pins only one branch and leaves the other as prose."""

    def test_open_pr_invoked_twice(self, by_id):
        body = by_id["coverage-watcher"].get("prompt_body", "") or ""
        # Count distinct invocations. Two PR scenarios = two
        # invocations expected (one each for fixable + tracking).
        count = body.count("scripts/orchestrator.py open-pr")
        assert count >= 2, (
            f"coverage-watcher prompt_body invokes open-pr "
            f"{count} time(s); expected ≥2 (one for the 'fixable "
            "in one fire' restore PR, one for the 'tracking PR' "
            "branch). Wiring only one branch leaves the other as "
            "prose for the LLM to assemble by hand"
        )


# ---------------------------------------------------------------------------
# Daily-digest: BOTH budget paths (low/medium + high/custom) must wire up
# ---------------------------------------------------------------------------


class TestDailyDigestBothBudgetPathsWired:
    """daily-digest has two PR-opening branches: the budget-check
    fast path (low/medium budget — runs the shell script then
    opens a PR) and the LLM path (high/custom — generates the
    digest then opens a PR). Both open PRs so both must wire."""

    def test_open_pr_invoked_twice(self, by_id):
        body = by_id["daily-digest"].get("prompt_body", "") or ""
        count = body.count("scripts/orchestrator.py open-pr")
        assert count >= 2, (
            f"daily-digest prompt_body invokes open-pr {count} "
            "time(s); expected ≥2 (one for the low/medium budget "
            "fast path, one for the high/custom LLM path). Each "
            "path independently opens a PR"
        )
