"""
Drift detectors for the `compliance-watcher` archetype — issue #81
(PRD #74).

Targets the "compliance foot-guns" class: source files missing
license headers, binary blobs committed to the repo, sensitive-data
patterns (SSN / credit-card / JWT / AWS-access-key) leaking into
files that `secret-scan` doesn't already catch.

Fires post-commit (git-hook primitive) and posts findings as a PR
review checklist on `notify` automation. Without this archetype,
compliance regressions slip through unchallenged — `secret-scan`
catches credentials but not license headers, file-size policy, or
PII patterns; `pr-review-bot` is generic and won't enforce these
specific rules.
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
        (a for a in catalog["archetypes"] if a["id"] == "compliance-watcher"),
        None,
    )
    assert arch is not None, (
        "compliance-watcher archetype is required (issue #81). "
        "Without it, license headers, file-size policy, and PII "
        "patterns slip through — `secret-scan` only handles "
        "credentials and `pr-review-bot` is too generic to enforce "
        "this rule set."
    )
    return arch


class TestComplianceWatcherShape:
    def test_primitive_is_git_hook(self, archetype):
        assert archetype["primitive"] == "git-hook", (
            f"compliance-watcher must fire on commit (post-commit "
            f"git-hook) so compliance violations are caught at the "
            f"earliest point; got {archetype['primitive']!r}"
        )

    def test_category_is_reactive(self, archetype):
        assert archetype.get("category") == "reactive", (
            "compliance-watcher reacts to a commit — it does not "
            "drive new feature work forward, so it belongs in the "
            "reactive lane"
        )

    def test_trigger_default_is_post_commit(self, archetype):
        """The spec pins post-commit. Without this pin, drift to
        scheduled or pre-commit would silently change the routine's
        semantics (when violations are caught, what blocks)."""
        trigger = archetype.get("trigger_default")
        # Trigger may be either a string ("on every git commit") or
        # a structured dict ({event: post-commit}). Accept both, but
        # the value must clearly reference commit / post-commit.
        s = (
            trigger
            if isinstance(trigger, str)
            else " ".join(f"{k}={v}" for k, v in (trigger or {}).items())
        ).lower()
        assert "commit" in s, (
            f"compliance-watcher trigger_default must be commit-"
            f"based (git-hook primitive); got {trigger!r}"
        )

    def test_automation_default_is_notify(self, archetype):
        """Spec: notify. Compliance violations need human judgment
        (a license header may need legal review; an SSN-shaped match
        may be a test fixture). Auto-rewriting would be wrong."""
        assert archetype["automation_default"] == "notify", (
            f"compliance-watcher must default to `notify` — "
            f"compliance violations need human judgment, not auto-"
            f"rewrites. Got {archetype['automation_default']!r}"
        )

    def test_self_evolve_is_false(self, archetype):
        assert archetype["self_evolve"] is False, (
            "compliance-watcher must NOT self_evolve — the rule "
            "catalog is the compliance contract; mid-run self-edits "
            "would silently weaken detection"
        )

    def test_success_criterion_uses_predicate_kind(self, archetype):
        sc = archetype["success_criterion"]
        assert isinstance(sc, dict), (
            f"compliance-watcher success_criterion must be a "
            f"structured predicate dict (issue #76 union); got "
            f"{type(sc).__name__}"
        )
        assert sc.get("kind") == "no-failures-n-days", (
            f"compliance-watcher success_criterion.kind must be "
            f"`no-failures-n-days`; got {sc.get('kind')!r}"
        )
        args = sc.get("args") or {}
        assert "days" in args, (
            "no-failures-n-days predicate requires args.days"
        )

    def test_stack_hints_are_broad_opt_in(self, archetype):
        """Spec: stack_hints: [] — compliance applies broadly, so
        users opt in deliberately rather than the interview auto-
        proposing it on every detected stack."""
        hints = archetype.get("stack_hints", None)
        assert hints == [] or hints is None or hints == [None], (
            f"compliance-watcher stack_hints must be empty (opt-in). "
            f"Compliance applies to any repo; the interview should "
            f"not auto-propose it based on stack signals. "
            f"Got {hints!r}"
        )


class TestComplianceWatcherPromptBody:
    """The rule catalog must be encoded in the prompt body so the
    routine actually looks for the right shapes. Without these pins
    the LLM will narrow to the easiest pattern (license headers) and
    skip the harder ones (PII regexes)."""

    def test_body_scans_diff_not_full_files(self, archetype):
        body = archetype["prompt_body"].lower()
        assert "diff" in body, (
            "compliance-watcher must scan the *diff* (the added lines "
            "of the commit), not full files — full-file scans would "
            "re-flag every pre-existing violation on every commit"
        )

    def test_body_references_license_header_rule(self, archetype):
        body = archetype["prompt_body"].lower()
        assert "license" in body and "header" in body, (
            "compliance-watcher prompt_body must reference the "
            "license-header rule; without an explicit mention the "
            "LLM may skip header enforcement entirely"
        )

    def test_body_references_file_size_cap(self, archetype):
        body = archetype["prompt_body"].lower()
        # Either `max_file_size_kb` (the config key) or any "file
        # size" / "size cap" / "binary blob" idiom — drift fails CI.
        assert (
            "max_file_size" in body
            or "file size" in body
            or "size cap" in body
            or "binary blob" in body
        ), (
            "compliance-watcher prompt_body must reference the file-"
            "size cap rule (max_file_size_kb / binary blobs) so the "
            "LLM doesn't drift to header-only enforcement"
        )

    def test_body_names_sensitive_patterns(self, archetype):
        """The PII / token patterns from issue #81 — SSN, credit
        card, JWT, AWS access key. Each must be named so the LLM
        cannot silently narrow to the easy ones."""
        body = archetype["prompt_body"].lower()
        required = [
            ("ssn", "social security"),
            ("credit card", "credit-card"),
            ("jwt",),
            ("aws", "access key"),
        ]
        missing = []
        for needle in required:
            if not any(n in body for n in needle):
                missing.append(needle)
        assert not missing, (
            f"compliance-watcher prompt_body must enumerate each "
            f"sensitive-data pattern (SSN, credit-card, JWT, AWS "
            f"access key) from issue #81; missing: {missing}. "
            f"Without explicit names the LLM will skip the less-"
            f"obvious ones."
        )

    def test_body_logs_findings(self, archetype):
        body = archetype["prompt_body"]
        # automation_default: notify means findings go to
        # log.jsonl — the prompt must say so explicitly or the LLM
        # may default to "do nothing" once it sees notify level.
        assert ".iteration/log.jsonl" in body, (
            "compliance-watcher must reference log.jsonl as the "
            "output surface — automation_level=notify means findings "
            "go to the log, and the prompt must say so explicitly"
        )
        assert "increment_signal" in body, (
            "compliance-watcher must reference increment_signal — "
            "the meta-agent's stagnation detection depends on it"
        )

    def test_body_describes_pr_review_output(self, archetype):
        """Spec: violations posted as a PR review with a checklist
        (one item per violation). The prompt must mention the PR
        review surface or the routine will only log silently."""
        body = archetype["prompt_body"].lower()
        assert "review" in body, (
            "compliance-watcher must reference a PR `review` "
            "surface — issue #81 spec: violations posted as a PR "
            "review checklist"
        )

    def test_body_references_routine_specific_inputs(self, archetype):
        """The rule set is configurable per-install (acceptance
        criterion: license template, max_file_size_kb,
        sensitive_patterns list). The prompt must reference reading
        config so the routine actually picks up overrides."""
        body = archetype["prompt_body"].lower()
        assert "config" in body or "routine_specific_inputs" in body, (
            "compliance-watcher must read its config — license "
            "template, max_file_size_kb, sensitive_patterns are "
            "user-tunable per acceptance criterion. Without a "
            "config reference the rules are hard-coded."
        )


class TestComplianceWatcherIsCommentOnly:
    """The routine writes findings (notify) and optionally posts a
    PR review — it does not branch + commit code. Must be in
    `COMMENT_ONLY_ARCHETYPES` so the global branch/commit body check
    exempts it. Otherwise the archetype is forced to grow a
    vestigial commit step that contradicts the notify semantics."""

    def test_listed_in_comment_only_set(self):
        from tests import test_catalog as t_catalog
        assert "compliance-watcher" in t_catalog.COMMENT_ONLY_ARCHETYPES, (
            "compliance-watcher emits findings rather than committing "
            "code (automation_default: notify) — it must be listed "
            "in `COMMENT_ONLY_ARCHETYPES` so the global branch/commit "
            "body check exempts it."
        )
