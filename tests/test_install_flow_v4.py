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


# ---------------------------------------------------------------------------
# Token-frugality: status script copy (PRD goal.md — token-frugality block)
# ---------------------------------------------------------------------------
# `Mode: status` runs `python3 scripts/status.py` from the consumer repo's
# working directory. The script ships inside the skill's package, NOT in
# the consumer repo. SKILL.md `Mode: status` already cross-references
# "Install copies scripts/status.py from the skill directory into the
# consumer repo (step 6a)" — but step 6a is silent on it. Result: a fresh
# install lands without the script, and `/auto-routines status` falls
# back to an LLM rendering and burns tokens on every call. Pin the
# instruction lives in step 6a so the cross-reference isn't a lie.

class TestInstallStep6aCopiesStatusScript:
    def test_step_6a_mentions_status_script(self, skill_text):
        """Step 6a must instruct the operator to copy scripts/status.py
        into the consumer repo. Without this, /auto-routines status
        consumes Claude tokens — the exact problem the script was
        created to fix."""
        step_6a_start = skill_text.find("**6a.")
        step_6b_start = skill_text.find("**6b.")
        assert step_6a_start != -1, "step 6a header missing"
        assert step_6b_start != -1, "step 6b header missing"
        step_6a = skill_text[step_6a_start:step_6b_start]
        assert "scripts/status.py" in step_6a, (
            "step 6a must reference scripts/status.py; Mode: status "
            "cross-references step 6a but the step itself is silent on it"
        )

    def test_step_6a_clarifies_source_is_skill_directory(self, skill_text):
        """The script lives in the skill's package, not the user's
        repo. Step 6a must say 'from the skill directory' (or an
        equivalent pointer) so the operator copies FROM the right
        place. Otherwise a fresh install can't find the source."""
        step_6a_start = skill_text.find("**6a.")
        step_6b_start = skill_text.find("**6b.")
        step_6a_lower = skill_text[step_6a_start:step_6b_start].lower()
        assert (
            "skill directory" in step_6a_lower
            or "skill package" in step_6a_lower
            or "skill's directory" in step_6a_lower
            or "from this skill" in step_6a_lower
        ), (
            "step 6a must clarify the script source is the skill "
            "directory, not the user's repo"
        )

    def test_step_6a_target_path_matches_mode_status_invocation(self, skill_text):
        """`Mode: status` runs `python3 scripts/status.py` from the
        consumer-repo root. Step 6a must land the copy at exactly
        `scripts/status.py` (relative to the consumer repo) — anywhere
        else and the Mode: status command fails."""
        step_6a_start = skill_text.find("**6a.")
        step_6b_start = skill_text.find("**6b.")
        step_6a = skill_text[step_6a_start:step_6b_start]
        # The Mode: status block invokes `python3 scripts/status.py`.
        # Step 6a must land the file at that same relative path.
        assert "scripts/status.py" in step_6a
        # And make the "relative to repo root" framing explicit so the
        # operator doesn't put it inside .iteration/ by accident.
        text_lower = step_6a.lower()
        assert (
            "repo root" in text_lower
            or "consumer repo" in text_lower
            or "relative to" in text_lower
        ), (
            "step 6a must clarify scripts/status.py lands relative to "
            "repo root, not inside .iteration/"
        )


# ---------------------------------------------------------------------------
# Skill UX: /auto-routines test-fire <routine_id> slash-command wrapper
# ---------------------------------------------------------------------------
# PRD `.iteration/goal.md` (Skill UX block) flagged `/auto-routines
# test-fire <routine_id>` as a missing operator-facing command. The
# orchestrator CLI already has the subcommand (PR #35), but SKILL.md
# never wired it to a Mode. Without a Mode entry, invoking
# `/auto-routines test-fire <id>` falls through to a generic LLM
# response — and the user pays Claude tokens for what should be a
# pure-script dispatch-plan print, exactly like Mode: status.

class TestModeTestFire:
    def test_modes_table_lists_test_fire(self, skill_text):
        """The Modes table at the top of SKILL.md is the operator's
        entry index. Without a row here, `test-fire` is undiscoverable
        and falls through to `init`/`status` heuristics."""
        # Find the Modes table.
        modes_start = skill_text.find("## Modes")
        guardrails_start = skill_text.find("## Guardrails")
        assert modes_start != -1 and guardrails_start != -1
        modes_table = skill_text[modes_start:guardrails_start]
        assert "test-fire" in modes_table, (
            "Modes table must list test-fire so the operator sees it "
            "as a first-class mode, not an undocumented escape hatch"
        )

    def test_mode_test_fire_section_exists(self, skill_text):
        """A dedicated `## Mode: test-fire <routine_id>` section must
        document the command. Without it, the slash-command wrapper
        has no behavior pinned and drifts toward an LLM render."""
        assert (
            "## Mode: `test-fire" in skill_text
            or "## Mode: `test-fire <routine_id>`" in skill_text
        ), "SKILL.md needs an explicit Mode: test-fire section"

    def test_mode_test_fire_is_pure_script(self, skill_text):
        """Same contract as Mode: status — no LLM, just shell out to
        `scripts/orchestrator.py test-fire`. The whole point is
        debugging without burning tokens or waiting for cron."""
        mode_start = skill_text.find("## Mode: `test-fire")
        if mode_start == -1:
            # Section missing — handled by the previous test; bail
            # out cleanly so the failure is attributable.
            return
        # Slice to the next top-level Mode section or end of file.
        rest = skill_text[mode_start + 1:]
        next_mode = rest.find("\n## Mode")
        next_section = rest.find("\n## ")
        ends = [e for e in (next_mode, next_section) if e != -1]
        section_end = min(ends) if ends else len(rest)
        section = skill_text[mode_start : mode_start + 1 + section_end]
        # Pin the no-LLM contract and the actual command shape.
        section_lower = section.lower()
        assert (
            "does not spawn" in section_lower
            or "no llm" in section_lower
            or "no claude tokens" in section_lower
            or "pure-script" in section_lower
            or "pure script" in section_lower
        ), "Mode: test-fire must declare it's a no-LLM pure-script mode"
        assert "scripts/orchestrator.py" in section, (
            "Mode: test-fire must invoke scripts/orchestrator.py "
            "test-fire (the existing CLI subcommand from PR #35)"
        )
        assert "test-fire" in section
        assert "--routine-id" in section or "<routine_id>" in section, (
            "Mode: test-fire must show the routine-id argument so the "
            "operator knows what to pass"
        )


# ---------------------------------------------------------------------------
# Skill UX: /auto-routines budget <tier> slash-command wrapper
# ---------------------------------------------------------------------------
# Same drift pattern as Mode: test-fire — PR #39 shipped the orchestrator
# `budget` subcommand, but SKILL.md never wired it to a Mode. The
# "Budget → cadence presets" prose at the bottom of SKILL.md describes
# the mapping table but never tells the operator HOW to re-apply it
# without re-running init. Without a Mode entry, `/auto-routines budget
# medium` falls through to an LLM render and burns tokens on what is
# already a pure-script command.

class TestModeBudget:
    def test_modes_table_lists_budget(self, skill_text):
        """The Modes table at the top of SKILL.md is the operator's
        entry index. Without a row here, `budget` is undiscoverable
        even though the CLI subcommand exists."""
        modes_start = skill_text.find("## Modes")
        guardrails_start = skill_text.find("## Guardrails")
        assert modes_start != -1 and guardrails_start != -1
        modes_table = skill_text[modes_start:guardrails_start]
        assert "`budget" in modes_table or "budget <tier>" in modes_table, (
            "Modes table must list budget <tier> so the operator sees "
            "it as a first-class mode, not an undocumented escape hatch"
        )

    def test_mode_budget_section_exists(self, skill_text):
        """A dedicated `## Mode: budget <tier>` section must document
        the command — its tier choices, what it rewrites, and the
        no-LLM contract."""
        assert (
            "## Mode: `budget" in skill_text
            or "## Mode: `budget <tier>`" in skill_text
        ), "SKILL.md needs an explicit Mode: budget section"

    def test_mode_budget_is_pure_script(self, skill_text):
        """Same contract as Mode: status and Mode: test-fire — no LLM,
        shells out to scripts/orchestrator.py budget. Re-applying the
        cadence preset table is a deterministic config rewrite, not
        an LLM task."""
        mode_start = skill_text.find("## Mode: `budget")
        if mode_start == -1:
            # Section missing — caught by test_mode_budget_section_exists.
            return
        rest = skill_text[mode_start + 1 :]
        next_mode = rest.find("\n## Mode")
        next_section = rest.find("\n## ")
        ends = [e for e in (next_mode, next_section) if e != -1]
        section_end = min(ends) if ends else len(rest)
        section = skill_text[mode_start : mode_start + 1 + section_end]
        section_lower = section.lower()
        assert (
            "does not spawn" in section_lower
            or "no llm" in section_lower
            or "no claude tokens" in section_lower
            or "pure-script" in section_lower
            or "pure script" in section_lower
        ), "Mode: budget must declare it's a no-LLM pure-script mode"
        assert "scripts/orchestrator.py" in section, (
            "Mode: budget must invoke scripts/orchestrator.py budget "
            "(the CLI subcommand shipped in PR #39)"
        )
        # Tier vocabulary must appear so the operator knows their
        # choices without re-reading the cadence-presets table.
        assert "low" in section_lower
        assert "medium" in section_lower
        assert "high" in section_lower
        # And cross-reference the existing test pin in
        # tests/test_orchestrator_cli.py::TestBudget so changes that
        # break the contract surface.
        assert (
            "TestBudget" in section
            or "test_orchestrator_cli" in section
            or "BUDGET_PRESETS" in section
        ), (
            "Mode: budget must cross-reference the existing test/code "
            "pin so the contract has a backstop"
        )
