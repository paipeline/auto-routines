"""
Tests for docs/first-24h.md.

PRD `.iteration/goal.md` (Documentation): "Write a 'first 24 hours'
walkthrough in `docs/first-24h.md`." These tests pin that the page
exists, names the canonical milestones a user hits in the first day
post-install, and shows what good output looks like — not just a
checklist.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "first-24h.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.exists(), (
        "docs/first-24h.md must exist — PRD goal.md (Documentation "
        "block) requires a 'first 24 hours' walkthrough"
    )
    return DOC.read_text()


# ---------------------------------------------------------------------------
# Existence + structure
# ---------------------------------------------------------------------------

class TestPageStructure:
    def test_doc_not_empty(self, doc_text):
        assert doc_text.strip(), "first-24h.md is empty"

    def test_doc_has_h1_title(self, doc_text):
        first_h1 = next(
            (ln for ln in doc_text.splitlines() if ln.startswith("# ")), None
        )
        assert first_h1 is not None
        first_lower = first_h1.lower()
        assert (
            "first" in first_lower
            and ("24" in first_lower or "day" in first_lower or "hour" in first_lower)
        ), f"first H1 should name the page; got {first_h1!r}"

    def test_doc_has_chronological_sections(self, doc_text):
        """The walkthrough should be ordered by elapsed time — the
        whole point is 'here is what happens hour-by-hour'."""
        h2s = [ln for ln in doc_text.splitlines() if ln.startswith("## ")]
        assert len(h2s) >= 3, (
            f"expected at least 3 H2 sections marking time milestones; "
            f"got {len(h2s)}: {h2s}"
        )


# ---------------------------------------------------------------------------
# Canonical milestones the walkthrough must cover
# ---------------------------------------------------------------------------
# A user lands on this page after `/auto-routines` finishes installing.
# The walkthrough is the contract for what they should expect to see —
# if any of these milestones drift out of the doc, the user is back to
# guessing whether the install "worked."

class TestPostInstallMilestone:
    """T+0: install just finished. What did it actually do?"""

    def test_mentions_iteration_dir(self, doc_text):
        """`.iteration/` is the install's ground truth on disk."""
        assert ".iteration/" in doc_text

    def test_mentions_config_file(self, doc_text):
        assert "config.yaml" in doc_text

    def test_mentions_per_routine_skills(self, doc_text):
        """The user should know SKILLs ship to `.claude/skills/<id>/`."""
        assert ".claude/skills" in doc_text


class TestFirstFireMilestone:
    """T+few-hours: the first scheduled routine fires. What does the
    user see?"""

    def test_mentions_log_jsonl(self, doc_text):
        """`.iteration/log.jsonl` is where the user reads what fired."""
        assert "log.jsonl" in doc_text

    def test_mentions_status_command(self, doc_text):
        """`/auto-routines status` is the canonical 'is it working?'
        command — it must appear in the walkthrough."""
        assert "/auto-routines status" in doc_text or "auto-routines status" in doc_text


class TestFirstPrMilestone:
    """T+hours-to-a-day: the first auto-PR lands. This is the
    whole-product moment — the README's '1–100 agents working on your
    repo' claim becomes literally true here."""

    def test_mentions_pr_branch_namespace(self, doc_text):
        """Routines push to `routines/<id>` — the user needs to know
        where to look for branches."""
        assert "routines/" in doc_text

    def test_mentions_gh_pr_list(self, doc_text):
        """`gh pr list` is how the user finds the first auto-PR."""
        assert "gh pr list" in doc_text


class TestEvolveMilestone:
    """T+~24h: the user wants to change something. `/auto-routines
    evolve` is the path."""

    def test_mentions_evolve_command(self, doc_text):
        assert "evolve" in doc_text.lower()


# ---------------------------------------------------------------------------
# Cross-links
# ---------------------------------------------------------------------------

class TestCrossLinks:
    def test_links_to_troubleshooting(self, doc_text):
        """If anything in the walkthrough breaks, the user should be
        one click away from troubleshooting.md — that's the whole
        reason both docs exist as a pair."""
        assert "troubleshooting" in doc_text.lower(), (
            "walkthrough should cross-link to docs/troubleshooting.md "
            "for failure recovery"
        )


# ---------------------------------------------------------------------------
# Actionable output
# ---------------------------------------------------------------------------

class TestActionableGuidance:
    def test_contains_command_blocks(self, doc_text):
        """A walkthrough without copy-pasteable commands is travel
        prose. The user wants to RUN something at each milestone."""
        bash_blocks = doc_text.count("```bash")
        assert bash_blocks >= 3, (
            f"expected at least 3 ```bash blocks (one per milestone); "
            f"found {bash_blocks}"
        )

    def test_shows_expected_output(self, doc_text):
        """At least one block should illustrate what good output
        looks like — otherwise the user can't tell if their install
        is healthy."""
        # Either a fenced block tagged `text`/`output`, or a sample
        # log/jsonl line (which the walkthrough should show).
        has_output_block = (
            "```text" in doc_text
            or "```output" in doc_text
            or "```jsonl" in doc_text
            or "```json" in doc_text
        )
        assert has_output_block, (
            "walkthrough must show at least one fenced example of what "
            "healthy output looks like (```text / ```output / "
            "```json / ```jsonl)"
        )
