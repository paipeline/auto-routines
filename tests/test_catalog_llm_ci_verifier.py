"""
Drift detectors for the `llm-ci-verifier` archetype — issue #79 (PRD #74).

Why this archetype matters: today `pr-review-bot` does generic
style/bug/security review — useful, but generic. `llm-ci-verifier`
binds review to the *intent* of the PR (the acceptance criteria of
the linked issue, or an inline checklist in the PR body itself). It
posts one inline review comment per failed criterion (or one summary
approval if all pass). This is the LLM CI verifier the README
implicitly promises.

Pinning the shape here so a future catalog edit can't quietly drop
the archetype or weaken its behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"


@pytest.fixture(scope="module")
def catalog() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def archetype(catalog) -> dict:
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "llm-ci-verifier"),
        None,
    )
    assert arch is not None, (
        "llm-ci-verifier archetype is required (issue #79). Without "
        "it, PR review stays generic — the README promises criteria-"
        "bound LLM CI."
    )
    return arch


class TestLlmCiVerifierShape:
    """Field-by-field pins from issue #79's acceptance criteria."""

    def test_primitive_is_pr_poll(self, archetype):
        assert archetype["primitive"] == "pr-poll", (
            f"llm-ci-verifier primitive must be `pr-poll` (open PRs "
            f"have no native webhook surface; we poll); got "
            f"{archetype['primitive']!r}"
        )

    def test_category_is_reactive(self, archetype):
        assert archetype.get("category") == "reactive", (
            "llm-ci-verifier reacts to existing PR diffs against the "
            "criteria — it doesn't drive new feature work forward"
        )

    def test_trigger_default_is_a_cron_phrase(self, archetype):
        # pr-poll requires trigger.cron once installed; the
        # archetype's `trigger_default` must be a phrase the express-
        # install mapper recognizes (every 15/30 min, etc.).
        trig = archetype["trigger_default"]
        assert isinstance(trig, str)
        assert "every" in trig.lower(), (
            f"llm-ci-verifier trigger_default must be a poll cadence "
            f"phrase like `every 15 minutes`; got {trig!r}"
        )

    def test_automation_default_does_not_self_merge(self, archetype):
        # The archetype posts review comments and may approve, but
        # must not auto-merge. Either `auto` (post comments, no
        # merge) or `notify` (print only) is acceptable — `notify`
        # is what issue #79 literally specifies, `auto` is what
        # makes the routine actually post comments. We allow both
        # so a follow-up tightening doesn't break this pin.
        assert archetype["automation_default"] in {"auto", "notify"}, (
            f"llm-ci-verifier automation_default must be `auto` "
            f"(post comments) or `notify` (print only); got "
            f"{archetype['automation_default']!r}. Never `suggest` "
            f"or `off` — neither matches the routine's purpose."
        )

    def test_self_evolve_is_false(self, archetype):
        assert archetype["self_evolve"] is False, (
            "llm-ci-verifier must not self_evolve — a bad mid-run "
            "evolve could silently lower the review bar"
        )

    def test_success_criterion_uses_predicate_kind(self, archetype):
        # Issue #79 spec: success_criterion uses the no-failures-n-days
        # predicate (issue #76). The catalog ships the dict form so
        # sanity-check's normalize_success_criterion accepts it as-is.
        sc = archetype["success_criterion"]
        assert isinstance(sc, dict), (
            f"llm-ci-verifier success_criterion must be a structured "
            f"predicate dict (issue #76 union); got {type(sc).__name__}"
        )
        assert sc.get("kind") == "no-failures-n-days", (
            f"llm-ci-verifier success_criterion.kind must be "
            f"`no-failures-n-days`; got {sc.get('kind')!r}"
        )
        args = sc.get("args") or {}
        assert isinstance(args, dict)
        assert "days" in args, (
            "llm-ci-verifier success_criterion.args must declare "
            "`days` (the no-failures window)"
        )

    def test_stack_hints_empty_or_universal(self, archetype):
        # Issue #79: applies to any stack with a CI system. Hints
        # should be empty so the interview proposes this archetype
        # regardless of language detection.
        hints = archetype.get("stack_hints") or []
        assert isinstance(hints, list)
        assert hints == [], (
            f"llm-ci-verifier stack_hints must be [] — the archetype "
            f"applies to any repo with PRs, regardless of language; "
            f"got {hints!r}"
        )


class TestLlmCiVerifierPromptBody:
    """The prompt_body must encode the criteria-bound review behavior
    — not drift to generic style review (that's pr-review-bot's job).
    """

    def test_body_reads_acceptance_criteria(self, archetype):
        body = archetype["prompt_body"].lower()
        assert "acceptance criteria" in body, (
            "llm-ci-verifier prompt_body must reference the "
            "`## Acceptance criteria` section it reads from the "
            "linked issue / PR body — without naming it, the prompt "
            "drifts back to generic review"
        )

    def test_body_reviews_diff_against_criteria(self, archetype):
        body = archetype["prompt_body"].lower()
        # Must explicitly tie the diff review to the criteria list,
        # not just "review the diff".
        assert "criter" in body and "diff" in body, (
            "llm-ci-verifier prompt_body must tie diff review to "
            "the criteria list — `review the diff` alone is what "
            "pr-review-bot already does"
        )

    def test_body_posts_inline_review_comments(self, archetype):
        body = archetype["prompt_body"].lower()
        # Per-criterion inline comments are the user-visible output.
        assert "inline" in body or "review comment" in body, (
            "llm-ci-verifier must post inline / line-anchored review "
            "comments — a single summary blob would lose the "
            "per-criterion granularity that's the whole point"
        )

    def test_body_names_gh_api_surface_for_review(self, archetype):
        body = archetype["prompt_body"]
        # Must name the actual gh CLI surface for posting reviews so
        # the routine doesn't fall back to plain `gh pr comment`
        # (which posts a top-level comment, not an inline review).
        assert "gh pr review" in body or "gh api" in body, (
            "llm-ci-verifier must name `gh pr review` or `gh api "
            ".../pulls/.../reviews` — the inline-review surface"
        )

    def test_body_logs_outcome(self, archetype):
        body = archetype["prompt_body"]
        # Every routine must log to log.jsonl. This is also pinned
        # globally in test_catalog.py — re-pin here so a regression
        # to "review and exit silent" fails this file too.
        assert "log" in body.lower() and "increment_signal" in body, (
            "llm-ci-verifier must log to .iteration/log.jsonl with "
            "`increment_signal` — same contract as every other "
            "archetype (the meta-agent reads it for stagnation)"
        )

    def test_body_forbids_auto_merge(self, archetype):
        body = archetype["prompt_body"].lower()
        # The routine posts comments — it must not merge the PR.
        # An explicit "do not merge" / "advisory" line keeps the
        # prompt from drifting toward "approve and merge" once it
        # sees green criteria.
        assert "merge" in body, (
            "llm-ci-verifier prompt_body must explicitly address "
            "merge behavior (e.g. `do not merge` / `advisory only`) "
            "— without it, an LLM seeing all green criteria might "
            "approve+merge unprompted"
        )


class TestLlmCiVerifierIsCommentOnly:
    """`llm-ci-verifier` posts review comments rather than commits;
    `test_catalog.py > COMMENT_ONLY_ARCHETYPES` must include it so
    the global `branch`/`commit` body check doesn't fail this
    archetype."""

    def test_listed_in_comment_only_set(self):
        # Import the live constant from the existing test module —
        # if the maintainer renames or relocates the set, this test
        # fails loud rather than silently going stale.
        from tests import test_catalog as t_catalog
        assert "llm-ci-verifier" in t_catalog.COMMENT_ONLY_ARCHETYPES, (
            "llm-ci-verifier posts review comments rather than "
            "committing code — it must be listed in "
            "`COMMENT_ONLY_ARCHETYPES` so the global "
            "`branch`/`commit` body check exempts it. Otherwise the "
            "archetype is forced to grow a vestigial commit step "
            "that has no real work to do"
        )
