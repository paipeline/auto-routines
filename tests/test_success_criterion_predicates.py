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
# Issue #76 — three additional structured kinds
# ---------------------------------------------------------------------------


class TestEvaluateCoverageAbove:
    """`coverage-above` reads a coverage file and compares the overall
    line-rate against `args.threshold` (percent, 0-100). Two source
    formats are supported:

    - Cobertura XML: `<coverage line-rate="0.84" ...>` (the format
      `pytest-cov --cov-report=xml` emits, the most common in repos
      we ship with).
    - `coverage report` plain-text stdout: a `TOTAL ... 84%` line at
      the bottom (the cheapest format to pipe into a file).

    The format is auto-detected from the file's first non-whitespace
    byte — `<` for XML, anything else for stdout. Missing / unreadable
    file returns False (predicate eval is observational)."""

    def test_xml_above_threshold_returns_true(self, orch, tmp_path):
        cov = tmp_path / "coverage.xml"
        cov.write_text(
            '<?xml version="1.0"?>\n'
            '<coverage line-rate="0.92" branch-rate="0.7" timestamp="0">\n'
            "</coverage>\n"
        )
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov), "threshold": 80},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True

    def test_xml_below_threshold_returns_false(self, orch, tmp_path):
        cov = tmp_path / "coverage.xml"
        cov.write_text('<?xml version="1.0"?>\n<coverage line-rate="0.62"/>\n')
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov), "threshold": 80},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False

    def test_xml_exactly_at_threshold_returns_true(self, orch, tmp_path):
        # `coverage-above` is inclusive — passing the threshold counts.
        # Otherwise a threshold of 80 and an actual of exactly 80%
        # would never complete, which is a footgun.
        cov = tmp_path / "coverage.xml"
        cov.write_text('<?xml version="1.0"?>\n<coverage line-rate="0.80"/>\n')
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov), "threshold": 80},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True

    def test_stdout_total_above_threshold_returns_true(self, orch, tmp_path):
        cov = tmp_path / "cov.txt"
        cov.write_text(
            "Name             Stmts   Miss  Cover\n"
            "--------------------------------------\n"
            "scripts/foo.py     120      5    96%\n"
            "scripts/bar.py      80      3    96%\n"
            "--------------------------------------\n"
            "TOTAL              200      8    96%\n"
        )
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov), "threshold": 80},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True

    def test_stdout_total_below_threshold_returns_false(self, orch, tmp_path):
        cov = tmp_path / "cov.txt"
        cov.write_text("TOTAL              200    100    50%\n")
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov), "threshold": 80},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False

    def test_missing_file_returns_false(self, orch, tmp_path):
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {
                    "file": str(tmp_path / "nope.xml"),
                    "threshold": 80,
                },
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False

    def test_default_threshold_is_80(self, orch, tmp_path):
        # When `threshold` is omitted, default to 80 — the round number
        # most repos quote. Pinning the default here means a config
        # missing the arg evaluates predictably.
        cov = tmp_path / "coverage.xml"
        cov.write_text('<?xml version="1.0"?>\n<coverage line-rate="0.85"/>\n')
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov)},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is True

    def test_unparseable_file_returns_false(self, orch, tmp_path):
        cov = tmp_path / "garbage.txt"
        cov.write_text("not a coverage report — just some text\n")
        routine = {
            "success_criterion": {
                "kind": "coverage-above",
                "args": {"file": str(cov), "threshold": 80},
            }
        }
        assert orch.evaluate_success_criterion(routine, context={}) is False


class TestEvaluatePrMergedCount:
    """`pr-merged-count` scans `.iteration/log.jsonl` for entries
    belonging to this routine with `outcome: ok` and a `pr_url`
    field. Returns True when the count meets or exceeds `args.count`.

    Scoping by routine id is critical — otherwise every routine in a
    multi-archetype install would 'complete' as soon as the global
    PR count crosses the threshold, regardless of which routine
    opened them."""

    @staticmethod
    def _write_log(path: Path, lines: list[dict]) -> None:
        import json
        with path.open("w", encoding="utf-8") as fh:
            for entry in lines:
                fh.write(json.dumps(entry) + "\n")

    def test_count_meets_threshold_returns_true(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/1"},
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/2"},
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/3"},
            ],
        )
        routine = {
            "id": "ci-watcher",
            "success_criterion": {
                "kind": "pr-merged-count",
                "args": {"count": 3},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine, context={"log_path": str(log)}
            )
            is True
        )

    def test_count_below_threshold_returns_false(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/1"},
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/2"},
            ],
        )
        routine = {
            "id": "ci-watcher",
            "success_criterion": {
                "kind": "pr-merged-count",
                "args": {"count": 5},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine, context={"log_path": str(log)}
            )
            is False
        )

    def test_other_routines_dont_contribute(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "other-1", "outcome": "ok", "pr_url": "p/1"},
                {"routine": "other-2", "outcome": "ok", "pr_url": "p/2"},
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/3"},
            ],
        )
        routine = {
            "id": "ci-watcher",
            "success_criterion": {
                "kind": "pr-merged-count",
                "args": {"count": 2},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine, context={"log_path": str(log)}
            )
            is False
        )

    def test_entries_without_pr_url_dont_count(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "ci-watcher", "outcome": "ok"},  # no pr_url
                {"routine": "ci-watcher", "outcome": "noop", "pr_url": "p/2"},  # not ok
                {"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/3"},
            ],
        )
        routine = {
            "id": "ci-watcher",
            "success_criterion": {
                "kind": "pr-merged-count",
                "args": {"count": 2},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine, context={"log_path": str(log)}
            )
            is False
        )

    def test_missing_log_returns_false(self, orch, tmp_path):
        routine = {
            "id": "ci-watcher",
            "success_criterion": {
                "kind": "pr-merged-count",
                "args": {"count": 1},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine, context={"log_path": str(tmp_path / "missing.jsonl")}
            )
            is False
        )

    def test_malformed_log_lines_are_skipped(self, orch, tmp_path):
        # A half-written log line shouldn't crash the predicate.
        log = tmp_path / "log.jsonl"
        log.write_text(
            '{"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/1"}\n'
            "this is not json\n"
            '{"routine": "ci-watcher", "outcome": "ok", "pr_url": "p/2"}\n'
        )
        routine = {
            "id": "ci-watcher",
            "success_criterion": {
                "kind": "pr-merged-count",
                "args": {"count": 2},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine, context={"log_path": str(log)}
            )
            is True
        )


class TestEvaluateNoFailuresNDays:
    """`no-failures-n-days` reads log.jsonl, filters to this routine,
    and returns True iff no entry in the last `args.days` days has
    `outcome: err`. Scoped to this routine (same as pr-merged-count).
    Empty window → False (no evidence of stability)."""

    @staticmethod
    def _write_log(path: Path, entries: list[dict]) -> None:
        import json
        with path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_all_ok_in_window_returns_true(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "ci", "outcome": "ok", "ts": "2026-05-09T10:00:00-0700"},
                {"routine": "ci", "outcome": "noop", "ts": "2026-05-10T10:00:00-0700"},
                {"routine": "ci", "outcome": "ok", "ts": "2026-05-11T10:00:00-0700"},
            ],
        )
        routine = {
            "id": "ci",
            "success_criterion": {
                "kind": "no-failures-n-days",
                "args": {"days": 7},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine,
                context={
                    "log_path": str(log),
                    "now": "2026-05-11T12:00:00-0700",
                },
            )
            is True
        )

    def test_err_in_window_returns_false(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "ci", "outcome": "ok", "ts": "2026-05-10T10:00:00-0700"},
                {"routine": "ci", "outcome": "err", "ts": "2026-05-11T08:00:00-0700"},
            ],
        )
        routine = {
            "id": "ci",
            "success_criterion": {
                "kind": "no-failures-n-days",
                "args": {"days": 7},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine,
                context={
                    "log_path": str(log),
                    "now": "2026-05-11T12:00:00-0700",
                },
            )
            is False
        )

    def test_old_err_outside_window_doesnt_count(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "ci", "outcome": "err", "ts": "2026-04-01T10:00:00-0700"},
                {"routine": "ci", "outcome": "ok", "ts": "2026-05-10T10:00:00-0700"},
            ],
        )
        routine = {
            "id": "ci",
            "success_criterion": {
                "kind": "no-failures-n-days",
                "args": {"days": 7},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine,
                context={
                    "log_path": str(log),
                    "now": "2026-05-11T12:00:00-0700",
                },
            )
            is True
        )

    def test_other_routine_errs_dont_contribute(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [
                {"routine": "other", "outcome": "err", "ts": "2026-05-11T08:00:00-0700"},
                {"routine": "ci", "outcome": "ok", "ts": "2026-05-11T09:00:00-0700"},
            ],
        )
        routine = {
            "id": "ci",
            "success_criterion": {
                "kind": "no-failures-n-days",
                "args": {"days": 7},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine,
                context={
                    "log_path": str(log),
                    "now": "2026-05-11T12:00:00-0700",
                },
            )
            is True
        )

    def test_no_entries_at_all_returns_false(self, orch, tmp_path):
        # No history → no evidence of stability → not yet "no failures
        # for N days". Avoid auto-completing a fresh install.
        log = tmp_path / "log.jsonl"
        log.write_text("")
        routine = {
            "id": "ci",
            "success_criterion": {
                "kind": "no-failures-n-days",
                "args": {"days": 7},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine,
                context={
                    "log_path": str(log),
                    "now": "2026-05-11T12:00:00-0700",
                },
            )
            is False
        )

    def test_only_entries_outside_window_returns_false(self, orch, tmp_path):
        log = tmp_path / "log.jsonl"
        self._write_log(
            log,
            [{"routine": "ci", "outcome": "ok", "ts": "2026-01-01T10:00:00-0700"}],
        )
        routine = {
            "id": "ci",
            "success_criterion": {
                "kind": "no-failures-n-days",
                "args": {"days": 7},
            },
        }
        assert (
            orch.evaluate_success_criterion(
                routine,
                context={
                    "log_path": str(log),
                    "now": "2026-05-11T12:00:00-0700",
                },
            )
            is False
        )


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
