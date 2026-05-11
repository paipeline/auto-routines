"""
Drift detectors: every `routines/<branch>` reference in an
archetype's prompt_body must match that archetype's own `id`.

Why this matters: archetypes name their working branch with the
convention `routines/<archetype-id>`. Across the catalog there are
two surfaces where this name appears:

  1. The `open-pr --head <branch>` invocation (Ticks 43+44 wired
     this for every PR-opening archetype).
  2. The branch-creation step (`git checkout -B routines/<id>`,
     `Branch: routines/<id>`, etc.).

If these drift — e.g. an archetype is renamed but only some of its
internal references update — the routine creates its commit on
branch `routines/old-name` but the PR invocation tries to open
against `routines/new-name`, which doesn't exist. The PR fails
loud but only after the routine has run.

This drift detector catches the desynchronization at catalog-load
time, not at routine-fire time. Two pins:

  A. Every `open-pr --head` value MUST equal `routines/<arch.id>`.
  B. Every `routines/<name>` reference in an archetype's
     prompt_body MUST equal `routines/<arch.id>` — unless the
     reference is part of a deliberate cross-archetype mention
     (e.g. meta-evolve describes what prd-implement does). The
     allowlist below records every legitimate cross-reference;
     anything outside it is drift.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"

# Cross-archetype references that are deliberate (one archetype
# describes another). Format: {referencing_archetype: {set of
# referenced archetype ids that are OK to appear in its body}}.
# Empty by default; populate only when a legitimate cross-mention
# is added.
CROSS_REFERENCE_ALLOWLIST: dict[str, set[str]] = {
    # meta-evolve queries GitHub for open `routines/prd-implement` PRs
    # to detect in-flight work it shouldn't rip out from tasks.md.
    # This is a deliberate cross-archetype reference (`gh pr list
    # --search "head:routines/prd-implement"`), not branch drift.
    "meta-evolve": {"prd-implement"},
    # pr-review-bot describes how it differs from pr-ci-watcher; that
    # comparison mentions the OTHER archetype's behavior, not a branch.
    # (It uses backtick-quoted names, not `routines/<id>`, so it
    # doesn't trip this detector anyway. Listed here for documentation.)
}


@pytest.fixture(scope="module")
def catalog() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text())


# ---------------------------------------------------------------------------
# A. open-pr --head must match routines/<archetype.id>
# ---------------------------------------------------------------------------


class TestOpenPrHeadMatchesArchetypeId:
    """Every `open-pr --head <branch>` invocation in an archetype's
    prompt_body must target `routines/<that-archetype's-id>`. Any
    mismatch is rename drift — the routine commits to one branch
    and tries to open a PR against a different one."""

    def test_every_open_pr_head_matches_archetype_id(self, catalog):
        offenders: list[tuple[str, str]] = []
        for arch in catalog["archetypes"]:
            body = arch.get("prompt_body", "") or ""
            aid = arch["id"]
            expected = f"routines/{aid}"
            # Find every open-pr invocation and its --head value.
            # The invocation can span lines (YAML block scalar), so
            # we use a permissive [\s\S]*? before --head.
            for m in re.finditer(
                r"open-pr[\s\S]{0,500}?--head\s+(\S+)", body
            ):
                head = m.group(1).rstrip("'`\"")
                if head != expected:
                    offenders.append((aid, head))
        assert not offenders, (
            "open-pr invocations target the wrong branch:\n"
            + "\n".join(
                f"  archetype={aid} --head={head}  (expected "
                f"routines/{aid})"
                for aid, head in offenders
            )
            + "\nThe wrapper's --head must match the archetype's "
            "branch-naming convention `routines/<id>`. A mismatch "
            "means the routine commits to one branch and opens a "
            "PR against a different one"
        )

    def test_archetypes_that_open_prs_have_at_least_one_invocation(
        self, catalog
    ):
        """Defensive check on the previous test: if no archetype
        invokes open-pr at all, the offenders list above would be
        empty and the test would pass vacuously. Pin that at least
        SOME archetype invokes open-pr, so a future regression that
        strips every invocation fails here too."""
        any_invocation = False
        for arch in catalog["archetypes"]:
            body = arch.get("prompt_body", "") or ""
            if re.search(r"open-pr[\s\S]{0,500}?--head\s+\S+", body):
                any_invocation = True
                break
        assert any_invocation, (
            "no archetype invokes `open-pr` with --head — Ticks "
            "43+44 wired the wrapper into 8 archetypes; if all of "
            "those went away, the wrapper rots unused and this "
            "test passes vacuously. Restore at least one invocation"
        )


# ---------------------------------------------------------------------------
# B. routines/<name> references must match the archetype's own id
# ---------------------------------------------------------------------------


class TestRoutinesBranchReferencesMatchArchetypeId:
    """For each archetype, any `routines/<name>` substring in its
    prompt_body must refer to itself — unless the name appears in
    CROSS_REFERENCE_ALLOWLIST. This catches the broader form of
    rename drift: not just open-pr invocations, but every branch
    reference (git checkout -B, `Branch:` lines, etc.)."""

    def test_every_routines_branch_reference_matches_id(self, catalog):
        offenders: list[tuple[str, str]] = []
        for arch in catalog["archetypes"]:
            body = arch.get("prompt_body", "") or ""
            aid = arch["id"]
            expected = f"routines/{aid}"
            allowed_others = CROSS_REFERENCE_ALLOWLIST.get(aid, set())
            allowed_full = {expected} | {
                f"routines/{x}" for x in allowed_others
            }
            seen = set(re.findall(r"routines/[a-z0-9-]+", body))
            unexpected = seen - allowed_full
            for ref in unexpected:
                offenders.append((aid, ref))
        assert not offenders, (
            "Branch references that don't match the archetype's "
            "id (and aren't in CROSS_REFERENCE_ALLOWLIST):\n"
            + "\n".join(
                f"  archetype={aid} reference={ref}"
                for aid, ref in offenders
            )
            + "\nIf one of these is a legitimate cross-archetype "
            "mention (e.g. archetype X describes what archetype Y "
            "does), add it to CROSS_REFERENCE_ALLOWLIST. Otherwise "
            "it's rename drift — fix the prompt_body"
        )


# ---------------------------------------------------------------------------
# C. Every open-pr-invoking archetype must also create its branch first
# ---------------------------------------------------------------------------


class TestOpenPrArchetypeCreatesBranchFirst:
    """If an archetype invokes `open-pr --head routines/<id>` but
    never instructs the routine to actually CHECK OUT or CREATE
    that branch (`git checkout -B routines/<id>` / `Branch:
    routines/<id>`), the routine commits to whatever branch HEAD
    happened to be on and then opens a PR against a phantom branch.
    Pin the precondition: every open-pr invocation has a branch-
    creation step nearby."""

    def test_every_open_pr_archetype_creates_its_branch(self, catalog):
        offenders: list[str] = []
        for arch in catalog["archetypes"]:
            body = arch.get("prompt_body", "") or ""
            aid = arch["id"]
            if "open-pr" not in body:
                continue
            expected = f"routines/{aid}"
            # Accept either `git checkout -B routines/<id>` or a
            # `Branch: routines/<id>` documentation line. Both are
            # in use across the catalog.
            has_checkout = re.search(
                rf"git checkout -B\s+{re.escape(expected)}", body
            )
            has_branch_line = re.search(
                rf"(?:^|\W)[Bb]ranch.{{0,5}}[`:]?\s*{re.escape(expected)}",
                body,
            )
            if not (has_checkout or has_branch_line):
                offenders.append(aid)
        assert not offenders, (
            f"These archetypes invoke `open-pr --head routines/<id>` "
            f"but never create the branch first: {offenders}. "
            "Without `git checkout -B routines/<id>` (or a `Branch: "
            "routines/<id>` step), the routine commits to whatever "
            "branch HEAD was on and then opens a PR against a "
            "phantom branch"
        )
