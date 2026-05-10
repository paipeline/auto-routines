"""
Tests for docs/troubleshooting.md.

PRD `.iteration/goal.md` (Documentation): "Add a troubleshooting page
covering the common install failures (`gh` not authed, MCP missing,
repo not yet pushed to remote)." These tests pin that the page exists
and covers each PRD-mandated failure mode with actionable guidance —
not just symptom prose.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "troubleshooting.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.exists(), (
        "docs/troubleshooting.md must exist — PRD goal.md "
        "(Documentation block) requires a troubleshooting page"
    )
    return DOC.read_text()


# ---------------------------------------------------------------------------
# Existence + structure
# ---------------------------------------------------------------------------

class TestPageStructure:
    def test_doc_exists(self, doc_text):
        assert doc_text.strip(), "troubleshooting.md is empty"

    def test_doc_has_h1_title(self, doc_text):
        first_h1 = next(
            (ln for ln in doc_text.splitlines() if ln.startswith("# ")), None
        )
        assert first_h1 is not None
        assert "troubleshoot" in first_h1.lower(), (
            f"first H1 should name the page; got {first_h1!r}"
        )

    def test_doc_references_diagnostic_files(self, doc_text):
        """The page must teach the operator to read `install-failed.md`
        and `halted.md` first — those files name the specific failure
        and shortcut the diagnostic flow."""
        assert "install-failed.md" in doc_text
        assert "halted.md" in doc_text


# ---------------------------------------------------------------------------
# PRD-mandated failure modes
# ---------------------------------------------------------------------------
# Three failure modes the PRD explicitly named:
#   1. `gh` not authenticated
#   2. MCP missing
#   3. Repo not yet pushed to remote
# Each must have its own section AND an actionable command block —
# symptom prose without a fix is not useful.

class TestGhNotAuthed:
    def test_section_exists(self, doc_text):
        text_lower = doc_text.lower()
        assert "gh" in text_lower and "auth" in text_lower
        # Look for either an H2 explicitly about gh auth, or a paragraph
        # heading mentioning it.
        assert (
            "gh not auth" in text_lower
            or "gh auth" in text_lower
            or "not authenticated" in text_lower
        ), "missing section on gh authentication failure"

    def test_section_has_fix_command(self, doc_text):
        """Must include the canonical fix command — `gh auth login`."""
        assert "gh auth login" in doc_text, (
            "gh-not-authed section must include the `gh auth login` "
            "command so the operator can copy-paste the fix"
        )


class TestMcpMissing:
    def test_section_exists(self, doc_text):
        text_lower = doc_text.lower()
        assert "mcp" in text_lower
        assert (
            "scheduled-tasks" in doc_text
            or "mcp server missing" in text_lower
            or "mcp not connected" in text_lower
            or "mcp missing" in text_lower
        ), "missing section on MCP-missing failure mode"

    def test_section_mentions_claude_mcp_list(self, doc_text):
        """`claude mcp list` is how the operator confirms which MCPs
        are connected — the section must reference it as the diagnostic
        command."""
        assert "claude mcp list" in doc_text, (
            "MCP section must reference `claude mcp list` as the "
            "diagnostic command"
        )


class TestRepoNotPushed:
    def test_section_exists(self, doc_text):
        text_lower = doc_text.lower()
        # "not pushed to remote" or "no remote pointing at github" — any
        # phrasing that names the failure.
        assert (
            "not yet pushed" in text_lower
            or "not pushed" in text_lower
            or "no remote" in text_lower
            or "remote pointing at github" in text_lower
        ), "missing section on repo-not-pushed-to-remote failure mode"

    def test_section_has_fix_commands(self, doc_text):
        """Must show the canonical fix path — `gh repo create` or
        `git remote add origin` + `git push -u origin main`."""
        assert "gh repo create" in doc_text or "git remote add origin" in doc_text, (
            "repo-not-pushed section must show how to create or attach "
            "the GitHub remote"
        )
        assert "git push" in doc_text, (
            "repo-not-pushed section must show the push command"
        )


# ---------------------------------------------------------------------------
# Additional install-time failures the page should cover
# ---------------------------------------------------------------------------
# These aren't PRD-mandated but they're the same family of common
# install halts. Worth pinning so the page doesn't drift to PRD-only
# coverage.

class TestAnthropicKeyMissing:
    def test_section_mentions_anthropic_api_key(self, doc_text):
        """The ANTHROPIC_API_KEY repo secret is the most common
        schema-4 install halt. Worth its own section."""
        assert "ANTHROPIC_API_KEY" in doc_text
        assert "gh secret" in doc_text, (
            "ANTHROPIC_API_KEY section must reference `gh secret set` "
            "/ `gh secret list`"
        )


class TestActionableGuidance:
    def test_doc_contains_command_blocks(self, doc_text):
        """A troubleshooting page without copy-pasteable commands is
        a symptom catalog. The PRD ask is for actionable guidance —
        every section in this page is expected to include a fenced
        bash block."""
        bash_blocks = doc_text.count("```bash")
        assert bash_blocks >= 3, (
            f"expected at least 3 ```bash code blocks (one per "
            f"PRD-mandated failure mode); found {bash_blocks}"
        )
