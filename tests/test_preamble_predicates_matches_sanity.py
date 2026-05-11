"""
Drift detector: the sealed predicate union for `success_criterion`
must agree across three surfaces:

  1. `scripts/orchestrator.py::PREDICATE_KINDS` — the evaluator's
     canonical set (issue #75 lands the foundation, #76 fills out).
  2. `scripts/sanity-check.py::PREDICATE_KINDS` — the validator that
     rejects invalid config.yaml before apply.
  3. `templates/routine-preamble.md::## Success criteria` — the docs
     users (and the LLM) read when authoring a routine.

If these drift, a user reads one truth (preamble) and the orchestrator
enforces another (PREDICATE_KINDS), or sanity-check rejects a kind
the orchestrator can actually evaluate. Same three-surface pattern as
`test_preamble_fsm_matches_sanity.py` and
`test_log_shape_pinned_to_canonical.py`.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from .conftest import sanity


ROOT = Path(__file__).resolve().parent.parent
PREAMBLE_PATH = ROOT / "templates" / "routine-preamble.md"


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


@pytest.fixture(scope="module")
def preamble_text() -> str:
    return PREAMBLE_PATH.read_text()


def _success_section(text: str) -> str:
    """Return the body of the `## Success criteria` preamble section
    (bounded to the next `## ` heading)."""
    m = re.search(r"^## Success criteria[^\n]*\n", text, re.M)
    assert m, (
        "preamble must have a `## Success criteria` section — without it, "
        "the predicate union is undocumented and this drift detector has "
        "nothing to bind to"
    )
    body = text[m.end():]
    # Bound to the next `## ` heading (or end of file).
    end = re.search(r"^## ", body, re.M)
    return body[: end.start()] if end else body


# ---------------------------------------------------------------------------
# orchestrator.PREDICATE_KINDS == sanity.PREDICATE_KINDS
# ---------------------------------------------------------------------------


class TestOrchestratorSanityPredicateKindsAgree:
    """The two enforcement surfaces (validator + evaluator) must agree
    on the exact set of allowed kinds. Validator-only or evaluator-only
    kinds are both broken: one rejects configs the other can serve,
    or accepts configs the other crashes on."""

    def test_both_constants_equal(self, orch):
        assert orch.PREDICATE_KINDS == sanity.PREDICATE_KINDS, (
            "orchestrator.PREDICATE_KINDS and sanity.PREDICATE_KINDS "
            "diverged. If you added a kind to the orchestrator's "
            "evaluator, mirror it in sanity-check.py — and vice versa."
        )


# ---------------------------------------------------------------------------
# Preamble enumerates every canonical kind
# ---------------------------------------------------------------------------


class TestPreambleEnumeratesEveryPredicateKind:
    """The preamble's `## Success criteria` section is the user-facing
    documentation of the union. Every kind in PREDICATE_KINDS must
    appear there verbatim — otherwise users (and the LLM) won't know
    which kinds are legal."""

    def test_every_canonical_kind_appears_in_preamble(self, orch, preamble_text):
        section = _success_section(preamble_text)
        for kind in orch.PREDICATE_KINDS:
            assert kind in section, (
                f"preamble `## Success criteria` is missing canonical "
                f"predicate kind {kind!r}. The orchestrator's "
                f"PREDICATE_KINDS is: {sorted(orch.PREDICATE_KINDS)}. "
                "Add the missing kind or drop it from the orchestrator."
            )

    def test_no_phantom_kinds_in_preamble(self, orch, preamble_text):
        """Inverse: the preamble must not name a kind the orchestrator
        doesn't actually handle. Catches a rename / removal drift."""
        section = _success_section(preamble_text)
        # Conservative tokenizer: predicate kinds are kebab-case
        # words bounded by backticks (the preamble formats them as
        # code spans). Look only inside `…` to avoid false positives
        # from regular prose.
        candidates = set(re.findall(r"`([a-z][a-z0-9-]{2,})`", section))
        # A few non-predicate kebab-cased tokens are mentioned in the
        # section legitimately (e.g. `success_criterion` itself,
        # `.iteration/goal.md` would be filtered by the underscore /
        # slash). Whitelist non-predicate tokens that look like
        # predicates but aren't.
        non_predicate_tokens = {"kind", "args", "success_criterion", "prose"}
        candidates -= non_predicate_tokens
        # Only flag a token as a phantom if it looks like a predicate
        # kind (kebab-case with at least one hyphen) AND is not in the
        # canonical set. Bare words like "file" or "threshold" are
        # arg names, not kinds.
        phantoms = {
            t for t in candidates
            if "-" in t and t not in orch.PREDICATE_KINDS
        }
        assert not phantoms, (
            f"preamble `## Success criteria` references phantom "
            f"predicate kind(s) {sorted(phantoms)} not in "
            f"orchestrator.PREDICATE_KINDS "
            f"({sorted(orch.PREDICATE_KINDS)}). If these were renamed "
            "or removed, update the preamble to match."
        )
