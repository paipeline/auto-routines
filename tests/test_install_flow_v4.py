"""
Tests for SKILL.md install flow updates (PRD #10 Module 6).

Schema 4 introduces five install-time obligations the prior flow doesn't
mention. SKILL.md is the source of truth the operator follows; if these
steps aren't documented there, they don't happen at install time, and
fresh installs end up with a config that fails the new sanity checks.

What schema 4 needs at install time (per PRD #10 Module 6):

  1. `.iteration/state.json` initialized via `state.initial_state()`.
  2. `.github/workflows/auto-routines.yml` written (PRD #10 Module 4).
  3. `ANTHROPIC_API_KEY` repo secret verified present (clear error if not).
  4. `_shared/preamble.md` rendered (PRD #10 Module 3 — already shipped
     in PR #11; this PR just makes sure the install step calls into it).
  5. Initial dashboard issue opened, its number recorded.
  6. Interview asks the user about the new schema-4 dials:
     `idle_window`, `idle_window_tz`, `gha_minutes_cap`.
  7. Verify step (step 7) asserts every scheduled / pr-poll routine has
     `execution_surface` set (sanity-check would catch this on the next
     evolve, but better to fail fast at install time).

These are content checks against SKILL.md — not test-of-execution.
A future improvement is to actually exercise the install flow end-to-end
in a temp repo, but that's a larger lift; this is the cheap fence.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = ROOT / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text():
    assert SKILL_PATH.exists(), "SKILL.md is the operator's manual; must exist"
    return SKILL_PATH.read_text()


# ---------------------------------------------------------------------------
# Install step content
# ---------------------------------------------------------------------------

class TestInstallStepArtifacts:
    def test_install_writes_state_json(self, skill_text):
        """Initial state.json from `state.initial_state()` is the ledger
        the orchestrator reads on every tick. Without it, first tick
        crashes (or, worse, silently bootstraps stale)."""
        assert "state.json" in skill_text
        # Must mention initialization, not just existence
        assert (
            "initial_state" in skill_text
            or "schema_version: 1" in skill_text
            or "state.initial_state" in skill_text
        )

    def test_install_writes_gha_workflow(self, skill_text):
        """Module 4 workflow YAML is the always-on execution surface.
        Install must write it (and must hard-fail if not a GitHub repo)."""
        assert ".github/workflows/auto-routines.yml" in skill_text

    def test_install_verifies_anthropic_api_key_secret(self, skill_text):
        """Without ANTHROPIC_API_KEY on the repo, the GHA workflow's
        headless Claude spawn step fails on every tick. Better to catch
        at install."""
        assert "ANTHROPIC_API_KEY" in skill_text
        # Should reference gh secret list / setting it up
        assert (
            "gh secret list" in skill_text
            or "gh secret set" in skill_text
            or "repo secret" in skill_text.lower()
        )

    def test_install_renders_preamble(self, skill_text):
        """PRD #10 Module 3 (PR #11) introduced `_shared/preamble.md`.
        The install flow must render it — otherwise schema-4 routine
        SKILLs reference a non-existent file."""
        assert "_shared/preamble.md" in skill_text or "preamble" in skill_text.lower()

    def test_install_opens_dashboard_issue(self, skill_text):
        """User story 18: dashboard issue opens on install + iter
        boundaries. Install seeds the first one."""
        # Either an explicit instruction or a step that mentions creating
        # the dashboard issue
        text_lower = skill_text.lower()
        assert (
            "dashboard issue" in text_lower
            or "open the iter" in text_lower
            or "auto-routines dashboard" in text_lower
        )


# ---------------------------------------------------------------------------
# Interview content — new schema-4 dials must be asked
# ---------------------------------------------------------------------------

class TestInterviewSchema4Dials:
    def test_interview_asks_about_idle_window(self, skill_text):
        """Idle window gates heavy autonomous work. Default `23:00-07:00`
        is reasonable, but the user must see + confirm + customize."""
        assert "idle_window" in skill_text

    def test_interview_asks_about_idle_window_tz(self, skill_text):
        """The IANA tz the idle_window resolves against. PRD #10 review:
        silent UTC fallback is the loudest footgun; don't ship without
        asking."""
        assert "idle_window_tz" in skill_text or "IANA" in skill_text

    def test_interview_asks_about_gha_cost_cap(self, skill_text):
        """User story 25: `meta.gha_minutes_cap` (default 60) must be
        offered so users on free tier don't accidentally drain budget."""
        assert "gha_minutes_cap" in skill_text


# ---------------------------------------------------------------------------
# Verify step (step 7) — schema-4 invariants
# ---------------------------------------------------------------------------

class TestVerifySchema4Invariants:
    def test_verify_checks_execution_surface(self, skill_text):
        """Every scheduled / pr-poll routine must declare `execution_surface`.
        Verify step should assert this so we don't ship a config that
        fails the next sanity check."""
        assert "execution_surface" in skill_text

    def test_verify_checks_workflow_file(self, skill_text):
        """Verify reads back .github/workflows/auto-routines.yml exists."""
        # Already covered by test_install_writes_gha_workflow but worth
        # a separate assertion: the verify step must mention it too.
        # Minimum bar: workflow path appears at least twice (install + verify).
        assert skill_text.count(".github/workflows/auto-routines.yml") >= 2

    def test_verify_checks_state_json_present(self, skill_text):
        """Verify step asserts state.json exists and has the right schema_version."""
        # state.json appears multiple times (files-this-skill-manages,
        # install step, verify step). At least 2.
        assert skill_text.count("state.json") >= 2


# ---------------------------------------------------------------------------
# Files-managed block — schema 4 additions
# ---------------------------------------------------------------------------

class TestFilesManagedBlock:
    def test_files_block_lists_state_json(self, skill_text):
        """The 'Files this skill manages' block must list state.json so
        users know it's part of the install footprint."""
        # The files block uses fenced code; just check it appears in
        # context with .iteration/
        assert ".iteration/state.json" in skill_text

    def test_files_block_lists_workflow(self, skill_text):
        assert ".github/workflows/auto-routines.yml" in skill_text


# ---------------------------------------------------------------------------
# Schema version pin
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    def test_skill_references_schema_4(self, skill_text):
        """SKILL.md must mention the current schema version so the
        operator knows which validator rules apply."""
        assert "schema_version: 4" in skill_text or "schema 4" in skill_text


# ---------------------------------------------------------------------------
# Local poller Stop hook wiring (PRD #10 OQ4 phase 5)
# ---------------------------------------------------------------------------
# The GHA workflow now appends to .iteration/local_dispatches.jsonl
# (PR #20) and a `local_poller.py poll` subcommand drains the queue
# (PRs #21, #22). The install must wire the Stop hook so the user
# doesn't have to figure this out manually — otherwise local-surface
# routines silently never fire on their machine.

class TestInstallStopHookForPoller:
    def test_install_mentions_local_poller_script(self, skill_text):
        """Install step must reference scripts/local_poller.py — that's
        where the hook command lives. Without a mention, the user has
        to discover the file by reading source."""
        assert "scripts/local_poller.py" in skill_text

    def test_install_uses_poll_subcommand(self, skill_text):
        """The hook should call `poll`, not `scan` or `fire` — only
        `poll` does watermark persistence (phase 4)."""
        # Look for `local_poller.py poll` together to pin the right
        # subcommand was documented.
        assert "local_poller.py poll" in skill_text

    def test_install_references_watermark_file_path(self, skill_text):
        """The poll command needs --watermark-file. Pin the canonical
        path so different installs converge on the same location."""
        assert ".iteration/.poller-watermark" in skill_text

    def test_install_references_local_dispatches_log(self, skill_text):
        """Same for --log: canonical path in install docs so users
        don't fork it."""
        assert ".iteration/local_dispatches.jsonl" in skill_text

    def test_install_documents_git_fetch_dance(self, skill_text):
        """Pollers need fresh log content from origin/main; without a
        git fetch step in the hook, the poller only ever sees what's in
        the working tree (which on a stale clone is nothing).
        Pin that the install mentions the fetch step."""
        # Either an explicit `git fetch` or a `git pull` in the hook
        # context is acceptable.
        text = skill_text
        assert "git fetch" in text or "git pull" in text, (
            "Stop-hook install must document how the poller picks up "
            "new dispatch log entries from origin/main (git fetch + "
            "checkout, or git pull in a worktree)"
        )

    def test_install_wires_stop_hook_for_poller(self, skill_text):
        """Per Module 4: local-fire dispatches reach the user via a
        Stop-hook poller. The install must add a Stop[] entry that runs
        the poller — separate from the always-on evolve-drain Stop hook."""
        # Look for a Stop hook entry that mentions the poller. Not too
        # strict on exact JSON shape — just that a Stop hook + the
        # poller script appear together within the install context.
        # Find the install section bounds.
        install_start = skill_text.find("### Install")
        verify_start = skill_text.find("### Verify")
        assert install_start != -1 and verify_start != -1
        install_section = skill_text[install_start:verify_start]
        assert "local_poller.py" in install_section
        assert "Stop" in install_section


class TestVerifyChecksPollerHook:
    def test_verify_section_mentions_poller(self, skill_text):
        """Verify step (step 7) should assert the poller hook exists.
        Without that check, install-failed.md is silent on a missing
        poller wire-up — the loudest possible foot-gun."""
        verify_start = skill_text.find("### Verify")
        assert verify_start != -1
        verify_section = skill_text[verify_start:]
        # Either explicit "local_poller" or "poller" mention in verify.
        assert "local_poller" in verify_section or "poller" in verify_section


class TestFilesManagedListsPollerArtifacts:
    def test_files_block_lists_dispatch_log(self, skill_text):
        """Operators should know local_dispatches.jsonl is part of the
        install footprint (it's committed back by the GHA workflow)."""
        assert "local_dispatches.jsonl" in skill_text

    def test_files_block_lists_watermark_file(self, skill_text):
        """And know that .poller-watermark is gitignored per-clone state."""
        assert ".poller-watermark" in skill_text
