"""
Drift detectors: SKILL.md install step 6k (the two-step iter commit)
must invoke `scripts/orchestrator.py checkpoint-append` instead of
hand-rolling the checkpoint row in prose.

Why this matters: the checkpoint-append wrapper (PR #56) was shipped
specifically to take the iter-number resolution (`max(existing)+1`,
not count) and timestamp formatting (local ISO with offset, never
UTC `Z`) out of LLM prose. As long as step 6k carries the literal
`printf 'iter-NNN: %s  %s\\n' ...` template, the LLM keeps
hand-formatting checkpoint rows and the wrapper rots unused — exactly
the failure mode #58 (render-routine-skill wire-up) and #64
(apply-fsm-plan wire-up) pinned for their wrappers.

Step 6k structure preserved: the two-step amend pattern stays. We're
only swapping out the line that writes the checkpoint row.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "SKILL.md"


def _step6k_block() -> str:
    r"""Return just step 6k's block. Bounded between
    `**6k. Two-step commit` and either the next `**6X.` heading (none
    today — 6k is the last sub-step) or the trailing `---` separator
    that ends step 6."""
    text = SKILL_MD.read_text()
    m = re.search(r"\*\*6k\. Two-step commit", text)
    assert m, (
        "Could not locate step 6k in SKILL.md. The header reads "
        "`**6k. Two-step commit (...)**`; if that section was "
        "renamed/moved, update this drift detector to match"
    )
    start = m.start()
    # Find the next step-6 sub-heading (`**6X.`) or the end-of-step-6
    # `---` separator, whichever comes first.
    after = text[m.end():]
    m_next_sub = re.search(r"\*\*6[a-z]\.", after)
    m_sep = re.search(r"^---$", after, re.M)
    candidates = [m_x.start() for m_x in (m_next_sub, m_sep) if m_x]
    end = m.end() + min(candidates) if candidates else len(text)
    return text[start:end]


# ---------------------------------------------------------------------------
# checkpoint-append must be invoked
# ---------------------------------------------------------------------------


class TestStep6kInvokesCheckpointAppend:
    def test_step6k_invokes_checkpoint_append(self):
        """The wire-up: step 6k.2 (the line that used to read
        `printf 'iter-001: %s  %s\\n' ...`) must invoke the
        deterministic wrapper. Without this, the LLM keeps
        hand-formatting iter numbers + timestamps."""
        block = _step6k_block()
        assert "checkpoint-append" in block, (
            "step 6k must invoke `scripts/orchestrator.py "
            "checkpoint-append` — the wrapper that handles iter-"
            "number resolution and ISO timestamp formatting. As "
            "long as the prose `printf 'iter-NNN: %s  %s\\n' ...` "
            "template lives here, the wrapper rots unused"
        )


# ---------------------------------------------------------------------------
# Required flags: --file, --sha, --summary
# ---------------------------------------------------------------------------


class TestStep6kRequiredFlags:
    """The wrapper takes three required flags. SKILL.md must show
    all three so the install procedure doesn't crash at argparse."""

    def test_passes_file_flag(self):
        block = _step6k_block()
        assert "--file" in block, (
            "step 6k must pass `--file` to checkpoint-append — "
            "the wrapper has no default checkpoints.md path"
        )

    def test_passes_sha_flag(self):
        block = _step6k_block()
        assert "--sha" in block, (
            "step 6k must pass `--sha` — the checkpoint's revert "
            "target; argparse-required"
        )

    def test_passes_summary_flag(self):
        block = _step6k_block()
        assert "--summary" in block, (
            "step 6k must pass `--summary` — the human-readable "
            "row label; argparse-required"
        )


# ---------------------------------------------------------------------------
# Path: targets the canonical checkpoints.md location
# ---------------------------------------------------------------------------


class TestStep6kFilePath:
    def test_targets_iteration_checkpoints_md(self):
        """Drift guard against someone changing the checkpoint path
        in step 6k but not the rest of the codebase. The canonical
        path is `.iteration/checkpoints.md`."""
        block = _step6k_block()
        assert ".iteration/checkpoints.md" in block, (
            "step 6k must target `.iteration/checkpoints.md` — "
            "the canonical checkpoints path pinned everywhere else "
            "in the codebase"
        )


# ---------------------------------------------------------------------------
# Two-step commit pattern preserved (we replace only the inner write)
# ---------------------------------------------------------------------------


class TestStep6kStillHasTwoStepCommit:
    """Step 6k's whole point is the amend trick: commit the install,
    then write checkpoints.md, then amend the row into the install
    commit. We're swapping the write step's mechanics, not the
    surrounding pattern."""

    def test_first_commit_still_present(self):
        block = _step6k_block()
        assert "git commit -m" in block, (
            "step 6k must still create the initial install commit — "
            "the amend trick depends on having a commit to amend"
        )
        assert "iter-001: install auto-routines" in block, (
            "the first commit's message must still be "
            "'iter-001: install auto-routines' — pinned by the "
            "self-host install commit message convention"
        )

    def test_amend_step_still_present(self):
        block = _step6k_block()
        assert "--amend" in block, (
            "step 6k.2 must still `git commit --amend` to fold "
            "the checkpoint row into the install commit — without "
            "this, checkpoints.md lands in a separate commit and "
            "the install becomes two commits instead of one"
        )

    def test_git_push_still_present(self):
        block = _step6k_block()
        assert "git push" in block, (
            "step 6k.3 must still push — the GHA workflow can't "
            "tick until the branch lands on GitHub"
        )


# ---------------------------------------------------------------------------
# Ordering: checkpoint-append before git commit --amend
# ---------------------------------------------------------------------------


class TestStep6kOrdering:
    """The amend folds checkpoints.md into the install commit. So
    checkpoint-append (which writes checkpoints.md) MUST run before
    `git commit --amend`. Reverse order would amend an empty file."""

    def test_checkpoint_append_appears_before_amend(self):
        block = _step6k_block()
        i_append = block.find("checkpoint-append")
        i_amend = block.find("--amend")
        assert i_append != -1 and i_amend != -1
        assert i_append < i_amend, (
            "`checkpoint-append` invocation must appear BEFORE "
            "`git commit --amend` in step 6k — the amend folds "
            "checkpoints.md into the install commit, so the file "
            "must be written first"
        )


# ---------------------------------------------------------------------------
# Prose hygiene: the old printf template must NOT survive
# ---------------------------------------------------------------------------


class TestStep6kProseHygiene:
    """If the printf-template line stays alongside the wrapper
    invocation, the LLM is likely to use it (verbose prose tends to
    win attention). The pin: the canonical hand-format string MUST
    be gone from step 6k."""

    def test_no_printf_iter_template(self):
        """Catch the canonical pre-wrapper form:
            printf 'iter-001: %s  %s\\n' "$SHA" "$(date +...)"
        """
        block = _step6k_block()
        # We look for the distinctive printf pattern. We accept that
        # 'printf' may appear in some unrelated context, but the
        # specific iter-NNN template format is the smell.
        forbidden_patterns = [
            r"printf 'iter-",
            r'printf "iter-',
        ]
        for pat in forbidden_patterns:
            assert not re.search(pat, block), (
                f"step 6k still contains a `{pat}` template line. "
                "With `checkpoint-append` wired, the prose printf "
                "template must be removed — keeping both invites "
                "the LLM to drift back to hand-formatting"
            )

    def test_no_date_offset_format_string(self):
        """The wrapper handles ISO-8601 with offset internally; the
        prose `$(date +%Y-%m-%dT%H:%M:%S%z)` line must be gone for
        the same reason."""
        block = _step6k_block()
        assert "date +%Y-%m-%dT%H:%M:%S" not in block, (
            "step 6k still hand-formats the timestamp via `date "
            "+%Y-%m-%dT%H:%M:%S%z`. The wrapper does this — leaving "
            "the prose form invites drift"
        )
