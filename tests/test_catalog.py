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
