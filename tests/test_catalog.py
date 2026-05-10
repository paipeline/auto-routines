"""
Schema tests for templates/routine-catalog.yaml.

The catalog is what makes auto-routines "actually do work" — every archetype
ships with a prompt_body that tells the routine to write code, commit, and
open a PR. These tests are the contract: they fail-loud when an archetype
drifts back toward "just print findings."
"""
from __future__ import annotations


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


VALID_CATEGORIES = {"reactive", "forward-driving"}


def test_every_archetype_has_category(catalog):
    """PRD goal.md (catalog quality): the interview groups archetypes in
    the candidate list as reactive (responds to events — commits, PRs,
    file edits) vs. forward-driving (proactively proposes work — drives
    a PRD/roadmap, scans for risk).

    Without an explicit `category` on every archetype, the interview
    can't group them sensibly and the user has no idea whether picking
    `prd-implement` will *do* things on a schedule or `commit-tests`
    will *react* to a commit.

    Pin every archetype to one of the two categories now so the
    interview consumer (when it lands) is unambiguous and so the
    distinction is visible in the catalog source itself."""
    for arch in catalog["archetypes"]:
        cat = arch.get("category")
        assert cat in VALID_CATEGORIES, (
            f"archetype {arch['id']} has invalid/missing category={cat!r} "
            f"(must be one of {sorted(VALID_CATEGORIES)})"
        )


def test_catalog_has_both_categories_represented(catalog):
    """The whole point of the categorization is to *contrast* — a catalog
    with only reactive archetypes (or only forward-driving) suggests the
    field is being mis-applied. Pin that both buckets are non-empty so
    we notice if a refactor accidentally collapses the distinction."""
    seen = {arch.get("category") for arch in catalog["archetypes"]}
    for required in VALID_CATEGORIES:
        assert required in seen, (
            f"no archetype declares category={required!r} — the "
            "reactive/forward-driving distinction in the catalog header "
            "becomes meaningless if one bucket is empty"
        )


def test_catalog_header_documents_category_field(catalog):
    """The YAML header block enumerates each archetype field with a
    one-liner so future contributors know what to fill in. If we add a
    field to the schema but forget to document it in the header, the
    next archetype author will skip it. Pin the header so the docs and
    the schema can't drift apart."""
    raw = CATALOG_PATH.read_text()
    # Take the leading comment block (lines starting with `#`) only — we
    # only want to assert against the documented field list, not the
    # archetype bodies (which may legitimately mention `category:` in a
    # comment somewhere).
    header_lines = []
    for line in raw.splitlines():
        if line.startswith("#") or line.strip() == "":
            header_lines.append(line)
        else:
            break
    header = "\n".join(header_lines)
    assert "category:" in header, (
        "templates/routine-catalog.yaml header block must document the "
        "`category:` field (one of reactive | forward-driving) so "
        "archetype authors know to set it"
    )
    # The header should also explain what the values mean — bare field
    # name with no explanation invites mis-categorization.
    header_lower = header.lower()
    assert "reactive" in header_lower and "forward-driving" in header_lower, (
        "header must mention both `reactive` and `forward-driving` so the "
        "distinction is visible at the top of the file"
    )


# Archetypes whose "real work" is posting comments rather than branch+commit.
# They still must log and use increment_signal.
COMMENT_ONLY_ARCHETYPES = {"pr-ci-watcher", "secret-scan", "pr-review-bot"}


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
    """The user-described routines from the bug reports must exist as archetypes:
    - the four maintenance routines (commit-tests, commit-lint, session-test-gap,
      session-doc-drift) — first bug report.
    - prd-implement — the "drive the project forward on a schedule" routine.
      Without this, the skill installs only reactive maintenance and never
      makes feature progress; that was the second bug report."""
    ids = {arch["id"] for arch in catalog["archetypes"]}
    for required in {
        "commit-tests", "commit-lint",
        "session-test-gap", "session-doc-drift",
        "prd-implement",
    }:
        assert required in ids, f"missing expected archetype: {required}"


def test_commit_tests_has_relevance_gates(catalog):
    """commit-tests fires on every commit, but for a repo that already has
    CI on push (which auto-routines does), running pytest on every WIP or
    docs-only commit is pure overhead — CI re-tests when the branch is
    pushed anyway. The archetype must include relevance gates so it earns
    its keep over CI rather than duplicating it.

    The audit (PRD #10 OQ5) called for two specific gates:
      1. Skip WIP commits (commit message matches `^WIP` or `^wip:`) —
         the user is mid-flow, doesn't want noise.
      2. Skip docs-only commits (HEAD touches only *.md, docs/, or other
         non-source paths) — pytest can't fail on prose changes.

    These keep commit-tests as a *fast local feedback loop* on real code
    changes (where CI is too slow to be useful) rather than redundant
    work on every commit."""
    arch = next(a for a in catalog["archetypes"] if a["id"] == "commit-tests")
    body = arch["prompt_body"].lower()

    # Gate 1: WIP
    assert "wip" in body, (
        "commit-tests must reference WIP-commit gating — otherwise it "
        "pytest's mid-flow checkpoints the user explicitly marked as "
        "incomplete (PRD #10 OQ5)"
    )

    # Gate 2: docs-only
    # Body should mention docs / *.md / non-source so the gate is actionable.
    assert any(token in body for token in ["docs-only", "docs/", "*.md", ".md", "non-source"]), (
        "commit-tests must reference docs-only gating — otherwise pytest "
        "runs on README edits and burns minutes for zero signal "
        "(PRD #10 OQ5)"
    )


def test_commit_tests_does_coverage_gap_fill(catalog):
    """PRD #10 user story 8 + OQ5 resolution: when tests pass, the routine
    must do the *value-add* over CI — find code paths the just-committed
    diff exposed but didn't cover, and open a PR adding tests for them.

    Without this, when the gates pass and tests are green, the routine
    exits noop and the user gets no value from the run. CI already proves
    green; this routine has to grow coverage to justify its minutes.

    The prompt body must reference:
      - Coverage measurement (e.g. pytest --cov, coverage diff, jest
        --coverage), so the action is concrete.
      - The gap-fill output: writing tests for uncovered paths exposed
        in the diff, opening a PR (not just logging the gap).
    """
    arch = next(a for a in catalog["archetypes"] if a["id"] == "commit-tests")
    body = arch["prompt_body"].lower()

    # Must reference coverage tooling — not just "tests pass"
    assert "coverage" in body, (
        "commit-tests must reference coverage tooling (pytest --cov, "
        "coverage, jest --coverage) — that's the value-add over CI's "
        "plain pytest run (PRD #10 user story 8, OQ5)"
    )

    # Must instruct writing tests for uncovered paths in the diff
    # (not just measuring or reporting the gap).
    assert "uncovered" in body or "coverage gap" in body or "diff coverage" in body, (
        "commit-tests must reference uncovered diff paths so the gap-fill "
        "action is unambiguous (PRD #10 OQ5: scope to coverage-gap PRs)"
    )


def test_commit_tests_acknowledges_ci_overlap(catalog):
    """The prompt body must explicitly note that CI also runs tests on
    push, so future maintainers don't strip out the gates thinking they're
    redundant safety. The gates exist *because* of the overlap, not in
    spite of it."""
    arch = next(a for a in catalog["archetypes"] if a["id"] == "commit-tests")
    body = arch["prompt_body"].lower()
    assert "ci" in body, (
        "commit-tests prompt_body must mention CI so the value-add over "
        "ci.yml is documented in the prompt itself (PRD #10 OQ5)"
    )


def test_meta_evolve_archetype_exists_with_goal_md_filter(catalog):
    """PRD #10 priority rule 4: when `.iteration/goal.md` changes, only
    `meta-evolve` should fire (re-plan iteration slices). The catalog
    must declare the archetype with:
      - primitive: git-hook (so it sits in the same dispatch lane as
        commit-tests/commit-lint, but priority-elevates via path_filters)
      - path_filters containing `.iteration/goal.md` so the orchestrator's
        match_trigger() short-circuit picks it
      - automation_default: auto (the user's goal edit means they want
        re-planning to happen, not a notification)

    Without this archetype the path_filters plumbing in
    scripts/orchestrator.py is dead code — there's no routine on the
    receiving end to consume the priority short-circuit.
    """
    archetypes = {a["id"]: a for a in catalog["archetypes"]}
    assert "meta-evolve" in archetypes, (
        "meta-evolve archetype is required by PRD #10 priority rule 4 — "
        "it's the routine that consumes the goal.md path-filter short-circuit"
    )
    arch = archetypes["meta-evolve"]
    assert arch["primitive"] == "git-hook", (
        "meta-evolve must be git-hook — same dispatch lane as commit-tests, "
        "elevated via path_filters not by a different primitive"
    )
    filters = arch.get("path_filters") or []
    assert ".iteration/goal.md" in filters, (
        f"meta-evolve must declare path_filters including '.iteration/goal.md' "
        f"so match_trigger short-circuits to it on goal edits; got {filters!r}"
    )
    assert arch["automation_default"] == "auto", (
        "meta-evolve must default to auto: a goal edit means re-plan now, "
        "not 'wait for the user to approve a notification'"
    )


def test_meta_evolve_replans_iteration_slices(catalog):
    """The meta-evolve prompt body must drive real re-planning work:
    read the new goal, diff it against `.iteration/tasks.md`, rewrite
    the task list, and commit. If the body is just 'analyze the diff',
    the routine drifts back to plan-only — same failure mode the rest
    of the catalog tests guard against."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "meta-evolve"),
        None,
    )
    assert arch is not None, "meta-evolve archetype required (see prior test)"
    body = arch["prompt_body"].lower()
    assert ".iteration/goal.md" in body, (
        "meta-evolve must read .iteration/goal.md — it's the source of truth "
        "for what re-planning means"
    )
    assert ".iteration/tasks.md" in body, (
        "meta-evolve must update .iteration/tasks.md — the cached task "
        "breakdown that other routines (prd-implement) consume"
    )
    # Must commit the result, not just print a diff.
    assert "branch" in body and "commit" in body, (
        "meta-evolve must branch + commit the rewritten tasks — analysis-only "
        "is the failure mode the catalog exists to prevent"
    )


def test_pr_review_bot_archetype_exists_with_correct_shape(catalog):
    """goal.md (Catalog quality): "Add a `pr-review-bot` archetype: posts
    inline review comments on open PRs (style, obvious bugs, security
    smells)."

    Differs from `pr-ci-watcher` (which only reacts to CI failures) and
    `secret-scan` (which only checks for credential leaks):
    `pr-review-bot` reviews the diff *itself* for style/bugs/smells and
    drops inline review comments on the lines that need attention.

    Shape:
      - scheduled (polls open PRs — no native PR webhook surface)
      - reactive (responds to existing PR diffs)
      - automation: auto (comments are advisory; safe to post without
        a second approval step)
      - self_evolve: false — review style should be stable so a bad
        mid-run evolve can't silently dumb down the review
    """
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "pr-review-bot"),
        None,
    )
    assert arch is not None, (
        "pr-review-bot archetype is required (goal.md Catalog quality) — "
        "without it, open PRs get no automated style/bug/smell review"
    )
    assert arch["primitive"] == "scheduled", (
        f"pr-review-bot primitive must be scheduled (poll open PRs), "
        f"got {arch['primitive']!r}"
    )
    assert arch.get("category") == "reactive", (
        "pr-review-bot is reactive — it reads existing PR diffs, doesn't "
        "drive new feature work"
    )
    assert arch["automation_default"] == "auto", (
        "pr-review-bot must default to auto — review comments are advisory; "
        "a notification step would just delay them"
    )
    assert arch["self_evolve"] is False, (
        "pr-review-bot must NOT self_evolve — a bad mid-run evolve could "
        "silently lower the review bar"
    )


def test_pr_review_bot_posts_inline_comments_not_just_summary(catalog):
    """The whole point vs. a generic summary comment is *inline* review
    comments: tied to specific lines so the author sees them in context.
    The prompt body must reference inline / line-anchored comments
    explicitly — bare 'leave a comment' would degrade to a summary."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "pr-review-bot"),
        None,
    )
    assert arch is not None, "pr-review-bot archetype required (see prior test)"
    body = arch["prompt_body"].lower()
    # Inline / line-anchored language
    assert any(token in body for token in ["inline", "review comment", "line", "anchor"]), (
        "pr-review-bot must mention inline / line-anchored comments — "
        "summary-only comments are what pr-ci-watcher already does"
    )
    # Must use the actual gh API for inline review comments —
    # `gh pr review --comment` and `gh api .../pulls/.../reviews` both
    # exist; the prompt should name at least one so the routine knows
    # how to actually post line-anchored feedback.
    assert "gh pr review" in body or "gh api" in body, (
        "pr-review-bot must name the gh CLI surface for posting inline "
        "review comments (`gh pr review` / `gh api`)"
    )


def test_pr_review_bot_covers_required_review_axes(catalog):
    """goal.md spells out three axes explicitly: 'style, obvious bugs,
    security smells'. Pin each so the prompt can't quietly narrow to
    one of them and call it done."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "pr-review-bot"),
        None,
    )
    assert arch is not None, "pr-review-bot archetype required (see prior test)"
    body = arch["prompt_body"].lower()
    assert "style" in body, (
        "pr-review-bot must reference style review (one of the three "
        "axes called out in goal.md)"
    )
    assert "bug" in body, (
        "pr-review-bot must reference bug review (one of the three axes)"
    )
    assert any(token in body for token in ["security", "smell", "vuln"]), (
        "pr-review-bot must reference security smells / vulnerabilities "
        "(one of the three axes)"
    )


def test_coverage_watcher_archetype_exists_with_correct_shape(catalog):
    """goal.md (Catalog quality): "Add a `coverage-watcher` archetype: opens
    a PR when project test coverage drops below threshold (per-language
    detection: pytest-cov, jest --coverage)."

    The archetype must:
      - exist in the catalog
      - run on a schedule (a coverage drop becomes visible only after
        tests run; polling the coverage report is the natural surface)
      - be reactive (it does NOT drive new features — it reacts to a
        regression in an existing metric)
      - default to `auto` automation — the response (open a PR adding
        tests, or a notification PR) is something the user will see
        and review either way
      - NOT self_evolve — the threshold and tooling are user-owned
        config; mid-run self-edits would silently change what counts
        as 'green'
    """
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "coverage-watcher"),
        None,
    )
    assert arch is not None, (
        "coverage-watcher archetype is required (goal.md Catalog quality) "
        "— without it, coverage regressions slip in unobserved"
    )
    assert arch["primitive"] == "scheduled", (
        f"coverage-watcher primitive must be scheduled (poll the coverage "
        f"report), got {arch['primitive']!r}"
    )
    assert arch.get("category") == "reactive", (
        "coverage-watcher is reactive: it responds to a metric regression, "
        "it doesn't drive new feature work forward"
    )
    assert arch["automation_default"] == "auto", (
        "coverage-watcher must default to auto — the user already opted "
        "into a coverage threshold, the response should not need a second "
        "approval step"
    )
    assert arch["self_evolve"] is False, (
        "coverage-watcher must NOT self_evolve — the threshold is user-owned "
        "config; mid-run self-edits would silently lower the bar"
    )


def test_coverage_watcher_names_concrete_tooling_and_threshold(catalog):
    """The prompt body must reference at least one concrete coverage tool
    per supported stack — generic 'measure coverage' lets the prompt drift
    to analysis-only. It must also name the threshold concept so the
    routine knows what counts as a regression."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "coverage-watcher"),
        None,
    )
    assert arch is not None, "coverage-watcher archetype required (see prior test)"
    body = arch["prompt_body"].lower()
    # Must name at least one concrete coverage tool per stack so the
    # routine actually runs something, not just 'check coverage'.
    assert "pytest" in body or "coverage" in body, (
        "coverage-watcher must reference pytest-cov / coverage.py for Python"
    )
    assert "jest" in body or "c8" in body or "nyc" in body, (
        "coverage-watcher must reference a JS/TS coverage tool "
        "(jest --coverage, c8, nyc) — goal.md called out per-language detection"
    )
    # Must reference a threshold so the routine knows what to react to.
    assert "threshold" in body, (
        "coverage-watcher must reference the coverage threshold — without "
        "a fail-bar, the routine has nothing to react to"
    )


def test_coverage_watcher_opens_pr_on_regression(catalog):
    """goal.md says explicitly: 'opens a PR when project test coverage
    drops below threshold'. The prompt body must mandate the PR (not
    just a comment or a log line), or the routine silently swallows
    regressions."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "coverage-watcher"),
        None,
    )
    assert arch is not None, "coverage-watcher archetype required (see prior test)"
    body = arch["prompt_body"].lower()
    # Standard branch/commit/PR chain — coverage-watcher is NOT in
    # COMMENT_ONLY_ARCHETYPES; it must do real branch+commit work.
    assert "branch" in body, "coverage-watcher must branch on regression"
    assert "commit" in body, "coverage-watcher must commit (tests added, or marker file)"
    assert "pr" in body or "pull request" in body, (
        "coverage-watcher must open a PR — that's the goal.md contract"
    )


def test_secret_scan_archetype_exists_and_polls_open_prs(catalog):
    """goal.md (Catalog quality): "Add a `secret-scan` archetype: catches
    leaked credentials in a PR and blocks merge with a comment."

    The archetype must:
      - exist in the catalog
      - poll open PRs (primitive=scheduled — there is no native PR webhook
        in the auto-routines surface, so a short-cadence poll is how
        secret-scan stays close to "as soon as the diff lands")
      - be reactive (responds to a PR existing with new commits, doesn't
        proactively scan unrelated history)
      - default to `auto` automation — silent on clean PRs, loud on
        leaked secrets; this is exactly the kind of action that should
        not require user approval
    """
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "secret-scan"),
        None,
    )
    assert arch is not None, (
        "secret-scan archetype is required (goal.md Catalog quality) — "
        "without it, leaked credentials in PRs go unchecked"
    )
    assert arch["primitive"] == "scheduled", (
        f"secret-scan primitive must be scheduled (poll open PRs), "
        f"got {arch['primitive']!r}"
    )
    assert arch.get("category") == "reactive", (
        "secret-scan is reactive — responds to existing PR diffs, doesn't "
        "drive new work forward"
    )
    assert arch["automation_default"] == "auto", (
        "secret-scan must default to auto — a leaked credential needs a "
        "loud, immediate response, not a notification the user might miss"
    )


def test_secret_scan_blocks_merge_and_names_concrete_patterns(catalog):
    """The prompt_body must encode the *blocking* behavior (post a
    comment + set a failing status check so the PR can't merge), and it
    must name concrete leak patterns so the routine doesn't fall back to
    a vague 'look for secrets'."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "secret-scan"),
        None,
    )
    assert arch is not None, "secret-scan archetype required (see prior test)"
    body = arch["prompt_body"].lower()
    # Must comment on the PR — the user-visible signal.
    assert "comment" in body, (
        "secret-scan must comment on the PR — that's the user-visible "
        "signal the routine produces"
    )
    # Must block merge — vague 'flag for review' is not enough.
    assert any(
        token in body
        for token in ["block", "status check", "merge", "fail"]
    ), (
        "secret-scan must reference blocking the merge (status check, "
        "failing review) — otherwise leaked credentials still merge"
    )
    # Must name concrete leak patterns so the routine actually looks for
    # the right shapes, not just 'inspect the diff'.
    assert any(
        token in body
        for token in [
            "aws_", "api key", "api_key", "token", "password",
            "private key", "credential",
        ]
    ), (
        "secret-scan prompt must name concrete leak patterns "
        "(AWS keys, API keys, tokens, passwords) — bare 'find secrets' "
        "lets prompts drift back to analysis-only"
    )


def test_secret_scan_does_not_self_evolve(catalog):
    """Security tooling that rewrites its own config is a foot-gun:
    one bad self-evolve and the scanner stops looking for a class of
    leaks. Pin it to fixed config."""
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "secret-scan"),
        None,
    )
    assert arch is not None, "secret-scan archetype required (see prior test)"
    assert arch["self_evolve"] is False, (
        "secret-scan must not self_evolve — security routines need fixed "
        "config so a bad mid-run evolve can't quietly disable detection"
    )


def test_prd_implement_drives_feature_work(catalog):
    """prd-implement is the routine that pushes feature work forward.
    It must be scheduled (not reactive), it must read .iteration/goal.md,
    and its body must mandate writing code + tests + PR (not just plans)."""
    arch = next(a for a in catalog["archetypes"] if a["id"] == "prd-implement")
    assert arch["primitive"] == "scheduled", (
        "prd-implement must be scheduled — that's the whole point: "
        "drive PRD forward without waiting for a commit"
    )
    body = arch["prompt_body"].lower()
    # Must read the goal
    assert ".iteration/goal.md" in body, (
        "prd-implement must read .iteration/goal.md as the PRD source"
    )
    # Must mandate code + tests, not just plans
    for phrase in ["write the failing test", "write the minimum code"]:
        assert phrase in body, (
            f"prd-implement body must include {phrase!r} — TDD is the "
            "guard against drifting back to 'plan only'"
        )
    # Must explicitly forbid plan-only output
    assert "do not plan-only" in body or "do not print findings" in body, (
        "prd-implement body must explicitly forbid plan-only output — "
        "that was the bug the archetype exists to fix"
    )


# ---------------------------------------------------------------------------
# Post-commit hook template
# ---------------------------------------------------------------------------

def test_post_commit_template_exists():
    assert HOOK_TEMPLATE.exists()


def test_post_commit_template_executable():
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

def test_routine_skill_template_has_no_double_bullet_at_routine_specific_inputs():
    """PRD #10 user story 28: 'fix the existing double-bullet bug in
    rendered SKILLs (line 20: `- - ...`)'.

    Root cause: the template wraps {{routine_specific_inputs}} in a
    leading `- ` ('- {{routine_specific_inputs}}'), but every entry in
    ROUTINE_SPECIFIC_INPUTS starts with '- ' for the first line. So the
    first rendered line is '- - foo' — the user-visible bug.

    Fix: drop the leading '- ' from the template, since each
    routine_specific_inputs value already supplies its own bullets.
    """
    text = ROUTINE_SKILL_TEMPLATE.read_text()
    # The `{{routine_specific_inputs}}` line MUST NOT have a leading
    # bullet — the substituted content brings its own.
    for line in text.splitlines():
        if "{{routine_specific_inputs}}" not in line:
            continue
        stripped = line.strip()
        assert not stripped.startswith("- "), (
            f"template line {line!r} prefixes "
            "{{routine_specific_inputs}} with a bullet — every rendered "
            "SKILL gets `- - ...` because the substituted value also "
            "leads with a bullet (PRD #10 user story 28)"
        )


def test_rendered_skills_have_no_double_bullets():
    """Belt-and-suspenders: rendered per-routine SKILLs must not contain
    `- - ` anywhere in their input list (the user-visible double-bullet
    bug from PRD #10 user story 28). If the template fix above lands but
    a renderer change later reintroduces the issue, this catches it."""
    skills_dir = ROOT / ".claude" / "skills"
    if not skills_dir.exists():
        pytest.skip("no rendered skills present (run scripts/render-routine-skills.py)")
    offenders = []
    for skill_md in skills_dir.glob("*/SKILL.md"):
        for n, line in enumerate(skill_md.read_text().splitlines(), 1):
            if line.startswith("- - "):
                offenders.append(f"{skill_md.relative_to(ROOT)}:{n}: {line}")
    assert not offenders, (
        "rendered SKILL.md files contain double-bullet lines (PRD #10 "
        "user story 28):\n" + "\n".join(offenders)
    )


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
