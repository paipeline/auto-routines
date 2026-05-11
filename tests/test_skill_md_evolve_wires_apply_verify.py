"""
Drift detectors: SKILL.md `Mode: evolve` step 4 must wire through the
deterministic `apply-fsm-plan` and `verify-fsm-state` wrappers shipped
in PRs #62 and #63 — not leave them as orphan tools the LLM has to
discover on its own.

Why these pins matter: a freshly-shipped pure-script wrapper is only
half the win. If SKILL.md still says "for every plan line, transition
the routine state" in prose, the LLM keeps editing config.yaml by
hand and the wrapper rots unused. The wrapper's whole point is to
take that step out of LLM prose; SKILL.md must invoke it explicitly.

The evolve pipeline SKILL.md must surface, in order:

    fsm-plan  →  apply-fsm-plan  →  verify-fsm-state

These tests bound their search to the `## Mode: evolve` section
specifically — a mention of `apply-fsm-plan` in some other section
(e.g. the orchestrator CLI reference, if we ever add one) doesn't
satisfy the wire-up requirement.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "SKILL.md"


def _evolve_mode_block() -> str:
    r"""Return just the `## Mode: evolve` section text, bounded by its
    own header and the next `## ` heading at the same level. We
    accept either `## Mode: evolve` or `## Mode: \`evolve\`` (the
    backtick form is used by other modes in this file)."""
    text = SKILL_MD.read_text()
    m = re.search(r"^## Mode: `?evolve`?\s*$", text, re.M)
    assert m, (
        "SKILL.md must expose a `## Mode: evolve` section — without "
        "it, the evolve flow has no entry-point Mode"
    )
    start = m.start()
    # Bound the section by the next top-level `## ` heading (any
    # heading at the same level, not just other Modes — the section
    # currently ends at `## Mid-run self-evolution`).
    nxt = re.search(r"^## ", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[start:end]


def _evolve_step4_block() -> str:
    """Step 4 specifically — the FSM-transitions step. The wire-up
    target. Bounded between `4. **Run automatic FSM transitions**`
    and the next numbered step (`5.`)."""
    block = _evolve_mode_block()
    m = re.search(r"^4\. \*\*Run automatic FSM transitions", block, re.M)
    assert m, (
        "Could not locate step 4 in `Mode: evolve`. The step's header "
        "starts with `4. **Run automatic FSM transitions`; if that "
        "rewording was intentional, update this drift detector to "
        "match the new header"
    )
    start = m.start()
    nxt = re.search(r"^5\. \*\*", block[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(block)
    return block[start:end]


# ---------------------------------------------------------------------------
# Pipeline must be present: fsm-plan → apply-fsm-plan → verify-fsm-state
# ---------------------------------------------------------------------------


class TestEvolveStep4Pipeline:
    """The evolve flow's automatic-FSM step must invoke all three
    pure-script legs deterministically. Missing any of them means
    the LLM is back to eyeballing the math."""

    def test_step4_invokes_fsm_plan(self):
        """fsm-plan was already wired (PR #53); regression guard."""
        block = _evolve_step4_block()
        assert "fsm-plan" in block, (
            "step 4 must invoke `fsm-plan` — the deterministic "
            "stagnation detector. Removing this invocation drops "
            "the deterministic ACTIVE→STAGNANT half of the FSM"
        )

    def test_step4_invokes_apply_fsm_plan(self):
        """apply-fsm-plan is the write half (PR #62). Without it,
        the FSM transitions live only as prose ("transition the
        routine state to STAGNANT") and the LLM hand-edits YAML."""
        block = _evolve_step4_block()
        assert "apply-fsm-plan" in block, (
            "step 4 must invoke `apply-fsm-plan` — the deterministic "
            "writer that consumes fsm-plan's JSONL and mutates "
            "routines[i].state atomically. Without it, the wrapper "
            "rots unused and config.yaml stays a hand-edit target"
        )

    def test_step4_invokes_verify_fsm_state(self):
        """verify-fsm-state is the read half (PR #63). Pinning it in
        step 4 means every evolve fire round-trips through apply +
        verify — half-applied configs become loud, not silent."""
        block = _evolve_step4_block()
        assert "verify-fsm-state" in block, (
            "step 4 must invoke `verify-fsm-state` — the read-side "
            "check that the apply actually landed. Without it, a "
            "half-applied or no-op apply goes undetected and the "
            "user sees stale state on the next fire"
        )


# ---------------------------------------------------------------------------
# Pipeline ordering: fsm-plan before apply before verify
# ---------------------------------------------------------------------------


class TestEvolveStep4Ordering:
    """Order matters: emit the plan, then apply it, then verify the
    apply landed. Any other order is nonsense (verify before apply
    asserts the pre-apply state; apply before plan has nothing to
    consume)."""

    def test_fsm_plan_appears_before_apply(self):
        block = _evolve_step4_block()
        idx_plan = block.find("fsm-plan")
        idx_apply = block.find("apply-fsm-plan")
        assert idx_plan < idx_apply, (
            "`fsm-plan` invocation must precede `apply-fsm-plan` in "
            "step 4 — apply consumes the plan emitted by fsm-plan, "
            "so the order has to match the pipeline direction"
        )

    def test_apply_appears_before_verify(self):
        block = _evolve_step4_block()
        idx_apply = block.find("apply-fsm-plan")
        idx_verify = block.find("verify-fsm-state")
        assert idx_apply < idx_verify, (
            "`apply-fsm-plan` must precede `verify-fsm-state` in "
            "step 4 — verify asserts the post-apply state, so it "
            "has to run after apply"
        )


# ---------------------------------------------------------------------------
# Invocations must pass the canonical config path
# ---------------------------------------------------------------------------


class TestEvolveStep4ConfigPath:
    """Both wrappers REQUIRE `--config`. SKILL.md must show the
    canonical path so the user-facing prose isn't ambiguous about
    which file is being read/written."""

    def test_apply_passes_config(self):
        block = _evolve_step4_block()
        # Find the apply-fsm-plan invocation block — bounded to its
        # own fenced or inline snippet. Cheap heuristic: the 200
        # chars surrounding `apply-fsm-plan` should mention
        # `--config` and `.iteration/config.yaml`.
        idx = block.find("apply-fsm-plan")
        assert idx != -1
        window = block[max(0, idx - 50):idx + 250]
        assert "--config" in window, (
            "`apply-fsm-plan` invocation must include `--config` — "
            "the wrapper has no cwd default, so an invocation "
            "without it crashes at argparse"
        )
        assert ".iteration/config.yaml" in window, (
            "`apply-fsm-plan` invocation must target "
            "`.iteration/config.yaml` — every other Mode pins this "
            "path; drift would make the install procedure inconsistent"
        )

    def test_verify_passes_config(self):
        block = _evolve_step4_block()
        idx = block.find("verify-fsm-state")
        assert idx != -1
        window = block[max(0, idx - 50):idx + 250]
        assert "--config" in window, (
            "`verify-fsm-state` invocation must include `--config` — "
            "same argparse contract as apply"
        )
        assert ".iteration/config.yaml" in window, (
            "`verify-fsm-state` invocation must target "
            "`.iteration/config.yaml`"
        )


# ---------------------------------------------------------------------------
# Both wrappers must reference the plan (file or stdin)
# ---------------------------------------------------------------------------


class TestEvolveStep4PlanWiring:
    """The whole point of the symmetric design (apply and verify
    consume the SAME plan file) is that step 4 chains them through a
    shared `--plan`. The pin: each invocation must include `--plan`."""

    def test_apply_references_plan_flag(self):
        block = _evolve_step4_block()
        idx = block.find("apply-fsm-plan")
        window = block[max(0, idx - 50):idx + 250]
        assert "--plan" in window, (
            "`apply-fsm-plan` invocation must include `--plan` — "
            "the wrapper requires it (file path or `-` for stdin)"
        )

    def test_verify_references_plan_flag(self):
        block = _evolve_step4_block()
        idx = block.find("verify-fsm-state")
        window = block[max(0, idx - 50):idx + 250]
        assert "--plan" in window, (
            "`verify-fsm-state` invocation must include `--plan` — "
            "matches apply's interface for symmetric piping"
        )


# ---------------------------------------------------------------------------
# Prose hygiene: no LLM-prose YAML edit instructions leak alongside
# ---------------------------------------------------------------------------


class TestEvolveStep4ProseHygiene:
    """If we ship the wrapper but leave the LLM-prose YAML-edit
    instructions ('mark the targeted routine `state: ACTIVE →
    STAGNANT`'), the model still does the hand-edit and ignores the
    wrapper. The prose must shift to "the wrapper applies the
    transition" — not "you, the LLM, apply the transition"."""

    def test_no_imperative_state_field_edit_prose_in_apply_zone(self):
        """The phrase 'transition the routine `state: ACTIVE →
        STAGNANT`' (or similar imperative-to-LLM forms) must NOT
        appear adjacent to the fsm-plan invocation. With the wrapper
        wired, the apply is the wrapper's job; the prose should
        describe what the wrapper does, not instruct the LLM."""
        block = _evolve_step4_block()
        # The deterministic-stagnation bullet that USED to say "For
        # every plan line, transition the routine `state: ACTIVE →
        # STAGNANT`" must now point at the wrapper. We check that
        # the imperative form is gone from the segment between
        # `fsm-plan` and the next bullet (`- **ACTIVE → COMPLETED`).
        i_plan = block.find("fsm-plan")
        # End of the stagnant bullet — start of the next FSM rule.
        m_next = re.search(r"^   - \*\*ACTIVE → COMPLETED",
                           block[i_plan:], re.M)
        if m_next:
            segment = block[i_plan:i_plan + m_next.start()]
        else:
            segment = block[i_plan:]
        # The wrapper does this — prose should not direct the LLM
        # to do it manually. Catch the canonical imperative form.
        forbidden = "For every plan line, transition the routine"
        assert forbidden not in segment, (
            "Step 4's deterministic-stagnation bullet still tells "
            "the LLM to hand-edit routine state. With apply-fsm-plan "
            "wired, the prose should describe what the wrapper "
            "does, not what the LLM should do. Rephrase to e.g. "
            "'apply the plan via apply-fsm-plan and verify it landed "
            "via verify-fsm-state.'"
        )
