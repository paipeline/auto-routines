"""
Tests for the structured `success_criterion` predicate union — issue #75.

Today `success_criterion` is freeform prose evaluated by the LLM-driven
`evolve` routine. This PRD #74 child slice introduces a sealed union of
predicate kinds, with `all-tasks-checked` as the first concrete kind.

Three orthogonal contracts are pinned here:

  1. `normalize_success_criterion(value)` auto-wraps any pre-existing
     prose string into `{kind: 'llm-narrative', args: {prose: <text>}}`
     so older config files keep loading without changes.

  2. `evaluate_success_criterion(routine_config, context)` returns a
     `bool` for structured kinds (orchestrator-enforced) and `None`
     for `llm-narrative` (deferred to the LLM evolve step).

  3. `all-tasks-checked` counts `[x]` vs `[ ]` checkboxes in a
     referenced markdown file and returns True iff every checkbox is
     checked AND there is at least one checkbox.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location(
        "orchestrator", ROOT / "scripts" / "orchestrator.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return _load_orchestrator()


# ---------------------------------------------------------------------------
# normalize_success_criterion — auto-wrap backward compat
# ---------------------------------------------------------------------------


class TestNormalizeSuccessCriterion:
    """Existing routine configs carry `success_criterion` as a free-text
    string. The new union expects a dict `{kind, args}`. The normalizer
    bridges both — so no config file edit is required to upgrade."""

    def test_prose_string_wraps_to_llm_narrative(self, orch):
        wrapped = orch.normalize_success_criterion(
            "all tasks in .iteration/goal.md marked done"
        )
        assert wrapped == {
            "kind": "llm-narrative",
            "args": {"prose": "all tasks in .iteration/goal.md marked done"},
        }

    def test_empty_string_wraps_to_empty_llm_narrative(self, orch):
        # An empty success_criterion is legal (routine runs indefinitely).
        # Normalize should round-trip without losing it.
        wrapped = orch.normalize_success_criterion("")
        assert wrapped == {"kind": "llm-narrative", "args": {"prose": ""}}

    def test_none_returns_none(self, orch):
        # Some configs omit the key entirely — the normalizer should not
        # invent a predicate where the user didn't write one.
        assert orch.normalize_success_criterion(None) is None

    def test_structured_dict_passes_through(self, orch):
        already = {"kind": "all-tasks-checked", "args": {"file": ".iteration/goal.md"}}
        assert orch.normalize_success_criterion(already) == already

    def test_structured_dict_with_missing_args_normalizes_to_empty_args(self, orch):
        # `kind: all-tasks-checked` alone (no args) is shorthand for
        # `args: {}` — the predicate evaluator supplies defaults.
        assert orch.normalize_success_criterion({"kind": "all-tasks-checked"}) == {
            "kind": "all-tasks-checked",
            "args": {},
        }

    def test_unknown_kind_raises(self, orch):
        # Defense in depth: a typo'd kind should error at normalize time
        # rather than silently behave as llm-narrative.
        with pytest.raises(ValueError) as exc:
            orch.normalize_success_criterion({"kind": "all-the-things"})
        assert "all-the-things" in str(exc.value)


# ---------------------------------------------------------------------------
# evaluate_success_criterion — orchestrator-enforced predicates
# ---------------------------------------------------------------------------


def _write_goal_md(tmp_path: Path, body: str) -> Path:
    goal = tmp_path / "goal.md"
    goal.write_text(body, encoding="utf-8")
    return goal


class TestEvaluateAllTasksChecked:
    """`all-tasks-checked` counts `[x]` / `[X]` vs `[ ]` checkboxes in
    the referenced markdown file. Goal completion when every checkbox
    is checked AND ≥1 checkbox exists. Mirrors `coverage-above:80%`
    semantics — a threshold predicate, not a "no items found" predicate."""

    def test_all_checked_returns_true(self, orch, tmp_path):
        goal = _write_goal_md(
            tmp_path,
            "# Goal\n\n- [x] write predicates\n- [X] add tests\n- [x] ship it\n",
        )
        routine = {
            "success_criterion": {
                "kind": "all-tasks-checked",
                "args": {"file": str(goal)},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True

    def test_partial_checked_returns_false(self, orch, tmp_path):
        goal = _write_goal_md(
            tmp_path,
            "- [x] done\n- [ ] not yet\n- [x] also done\n",
        )
        routine = {
            "success_criterion": {
                "kind": "all-tasks-checked",
                "args": {"file": str(goal)},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False

    def test_no_tasks_returns_false(self, orch, tmp_path):
        # Empty goal file → not "done" — there's nothing to be done yet.
        # Returning True here would auto-complete every freshly-installed
        # routine before the user has written their goal.
        goal = _write_goal_md(tmp_path, "# Goal\n\n(nothing yet)\n")
        routine = {
            "success_criterion": {
                "kind": "all-tasks-checked",
                "args": {"file": str(goal)},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False

    def test_missing_file_returns_false(self, orch, tmp_path):
        # Goal file not yet created. Returning False (not raising) keeps
        # the orchestrator's tick path total — predicate eval is a
        # best-effort observation, not a contract assertion.
        routine = {
            "success_criterion": {
                "kind": "all-tasks-checked",
                "args": {"file": str(tmp_path / "nonexistent.md")},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False

    def test_default_file_is_iteration_goal_md(self, orch, tmp_path, monkeypatch):
        # When args.file is omitted, the predicate defaults to
        # `.iteration/goal.md` resolved from cwd. Pin this so a renamed
        # default doesn't silently break old configs.
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".iteration").mkdir()
        (tmp_path / ".iteration" / "goal.md").write_text("- [x] done\n")
        routine = {
            "success_criterion": {
                "kind": "all-tasks-checked",
                # no args.file
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True

    def test_indented_checkboxes_also_count(self, orch, tmp_path):
        # Nested task lists with leading whitespace are common in PRDs.
        goal = _write_goal_md(
            tmp_path,
            "- [x] top\n  - [x] sub\n  - [x] sub2\n",
        )
        routine = {
            "success_criterion": {
                "kind": "all-tasks-checked",
                "args": {"file": str(goal)},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True


class TestEvaluateLlmNarrative:
    """`llm-narrative` is the catch-all kind for unstructured criteria.
    The orchestrator can't evaluate it — return None so the caller
    routes it through the LLM evolve step."""

    def test_llm_narrative_returns_none(self, orch):
        routine = {
            "success_criterion": {
                "kind": "llm-narrative",
                "args": {"prose": "0 high vulns for 4 consecutive runs"},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is None

    def test_freeform_prose_normalized_then_evaluated(self, orch):
        # End-to-end: a config that still carries prose should normalize
        # through and then return None from the evaluator.
        routine = {"success_criterion": "0 vulns 4 runs running"}
        normalized = orch.normalize_success_criterion(routine["success_criterion"])
        routine["success_criterion"] = normalized
        assert orch.evaluate_success_criterion(routine, context={}) is None


class TestEvaluateMissingOrEmpty:
    """A routine with no `success_criterion` (or empty prose) runs
    indefinitely — the orchestrator never auto-completes it. Return
    None to signal 'no predicate'."""

    def test_no_success_criterion_returns_none(self, orch):
        assert orch.evaluate_success_criterion({}, context={}) is None

    def test_empty_string_success_criterion_returns_none(self, orch):
        # Auto-wrapped empty prose is still semantically "no criterion".
        routine = {
            "success_criterion": {"kind": "llm-narrative", "args": {"prose": ""}}
        }
        assert orch.evaluate_success_criterion(routine, context={}) is None


class TestEvaluateUnknownKind:
    """Defense in depth: if some path produced an invalid kind despite
    normalize_success_criterion validating, the evaluator must refuse
    rather than guess. Sealed union — exhaustive match or bust."""

    def test_unknown_kind_raises(self, orch):
        routine = {"success_criterion": {"kind": "all-the-things", "args": {}}}
        with pytest.raises(ValueError) as exc:
            orch.evaluate_success_criterion(routine, context={})
        assert "all-the-things" in str(exc.value)


# ---------------------------------------------------------------------------
# PREDICATE_KINDS constant is exposed for sanity-check / drift binding
# ---------------------------------------------------------------------------


class TestPredicateKindsConstant:
    """The orchestrator's PREDICATE_KINDS frozenset is the canonical
    source of truth for which kinds the evaluator handles. The
    sanity-check enforcer mirrors it; the preamble docs enumerate it."""

    def test_contains_all_tasks_checked_and_llm_narrative(self, orch):
        assert "all-tasks-checked" in orch.PREDICATE_KINDS
        assert "llm-narrative" in orch.PREDICATE_KINDS

    def test_contains_the_full_union_from_prd(self, orch):
        # The PRD specifies the full sealed union. Issue #75 lands
        # `all-tasks-checked` + `llm-narrative` as evaluators; the rest
        # are declared (so sanity-check accepts them) and implemented
        # in issue #76. Pinning here means #76 only needs to add the
        # evaluator branches — not also touch this constant.
        assert orch.PREDICATE_KINDS == frozenset(
            {
                "all-tasks-checked",
                "coverage-above",
                "pr-merged-count",
                "no-failures-n-days",
                "llm-narrative",
            }
        )

    def test_is_frozenset(self, orch):
        # Immutable so a downstream import can't accidentally mutate
        # the union and let invalid kinds slip past validation.
        assert isinstance(orch.PREDICATE_KINDS, frozenset)
