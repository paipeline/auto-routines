"""
Tests for the shared per-routine preamble extraction.

PRD `.iteration/goal.md` (Token frugality):
    "Trim the per-routine SKILL.md preamble. The current rendered template
    is ~3KB of boilerplate per fire; extract the FSM/state-handling section
    into a single shared file the routine can `cat` once at start."

The slice ships:
  - `templates/routine-preamble.md` — the canonical shared protocol
    every routine references (commit/PR procedure, log line format,
    state handling, failure modes). Identical bytes across every
    routine fire — so it's cache-hit-able and editable in one place.
  - `templates/routine-skill.md` — trimmed to ROUTINE-SPECIFIC content
    (name, purpose, trigger, prompt_body, self-evolve) plus a single
    `## Reference` pointer at `.claude/skills/_shared/preamble.md`.
  - SKILL.md install step 6f already plans to render the preamble;
    pin that the references in step 6f and the "Schema-4 install
    artifacts" verification all name the same paths.

These tests are the fence around the refactor — if someone later
re-duplicates boilerplate into the per-routine template, the drift
detectors fail.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PREAMBLE_TEMPLATE = ROOT / "templates" / "routine-preamble.md"
ROUTINE_SKILL_TEMPLATE = ROOT / "templates" / "routine-skill.md"
SKILL_MD = ROOT / "SKILL.md"

REFERENCE_DEST_PATH = ".claude/skills/_shared/preamble.md"


# ---------------------------------------------------------------------------
# Preamble template exists and covers the right surface
# ---------------------------------------------------------------------------


class TestPreambleExists:
    def test_preamble_template_file_exists(self):
        """The whole slice is meaningless if the file isn't there.
        SKILL.md step 6f already references `templates/routine-preamble.md`;
        without this file, the install procedure crashes."""
        assert PREAMBLE_TEMPLATE.exists(), (
            f"missing {PREAMBLE_TEMPLATE.relative_to(ROOT)} — SKILL.md "
            f"step 6f says to render this file but it doesn't exist; the "
            f"install procedure would fail on a fresh repo"
        )

    def test_preamble_has_no_placeholders(self):
        """The preamble is TRULY shared across every routine — no
        per-routine substitution happens. If there's a `{{...}}` in
        it, the rendered file lands with unfilled placeholders and
        the install verification at SKILL.md line 380 fails."""
        text = PREAMBLE_TEMPLATE.read_text()
        placeholders = re.findall(r"\{\{[^}]+\}\}", text)
        assert placeholders == [], (
            f"preamble contains unfilled placeholders {placeholders} — "
            f"the shared preamble must be pure shared content; anything "
            f"per-routine belongs in templates/routine-skill.md instead"
        )


class TestPreambleCoversCoreSections:
    """The 4 sections we extracted out of the per-routine template all
    have to be present in the preamble — otherwise we just deleted
    rules without relocating them."""

    def test_covers_commit_and_pr_procedure(self):
        """Every routine that produces a diff must commit + push +
        open a PR. If this section is missing, automation_level=auto
        routines silently stop opening PRs."""
        text = PREAMBLE_TEMPLATE.read_text().lower()
        assert "git commit" in text or "git checkout" in text, (
            "preamble must describe the commit procedure"
        )
        assert "gh pr" in text, (
            "preamble must describe how routines open PRs"
        )
        assert "routines/" in text, (
            "preamble must name the canonical branch prefix "
            "(`routines/<routine_id>`)"
        )

    def test_covers_log_line_format(self):
        """The .iteration/log.jsonl format must be defined SOMEWHERE
        canonical — readers (status.py, dashboards, post-commit hook
        sandbox tests) all assume the same shape."""
        text = PREAMBLE_TEMPLATE.read_text().lower()
        assert "log.jsonl" in text, (
            "preamble must name the log file path"
        )
        # The 6 fields that downstream readers depend on:
        for field in ("ts", "routine", "outcome", "summary",
                      "increment_signal", "last_fire_sha"):
            assert field in text, (
                f"preamble must name log field {field!r} — at least one "
                f"downstream reader expects it"
            )

    def test_covers_state_handling(self):
        """ACTIVE / EVOLVING fire; everything else noops. If this
        rule isn't in the preamble, a STAGNANT routine could
        accidentally produce work on the next fire."""
        text = PREAMBLE_TEMPLATE.read_text()
        # The full FSM vocabulary the preamble has to know about.
        for state in ("ACTIVE", "EVOLVING", "STAGNANT",
                      "COMPLETED", "STOPPED"):
            assert state in text, (
                f"preamble must mention {state!r} so the routine knows "
                f"which states fire vs. noop"
            )

    def test_covers_failure_modes(self):
        """Missing-dep / time-budget-exceeded / never-silently-swallow
        — these are universal failure-handling rules. Without them
        in the preamble, every routine reinvents (and gets wrong)
        error logging."""
        text = PREAMBLE_TEMPLATE.read_text().lower()
        # "missing dep" rule and "log before exit" rule must both
        # have some surface.
        assert "missing dep" in text or "missing dependency" in text, (
            "preamble must describe the missing-dep failure mode"
        )
        assert "log" in text and "exit" in text, (
            "preamble must spell out the 'log before exiting on error' rule"
        )


class TestPreambleLogFormatIsValidJSON:
    """Beyond having the field names, the example log block in the
    preamble should actually be valid (or fenced-as-illustrative)
    JSON. Otherwise a routine copy-pastes it and gets a parse error."""

    def test_example_log_block_is_in_a_fenced_code_block(self):
        text = PREAMBLE_TEMPLATE.read_text()
        # We don't need to fully parse — just pin that there's a
        # ```json...``` block somewhere with the routine field in it.
        m = re.search(r"```json\s*\n([\s\S]*?)\n```", text)
        assert m, (
            "preamble must contain a ```json fenced code block showing "
            "the log line shape — readers copy-paste it"
        )
        block = m.group(1)
        assert "routine" in block and "outcome" in block, (
            "the fenced log block must illustrate the canonical fields"
        )


# ---------------------------------------------------------------------------
# routine-skill.md trimmed correctly — drift detector
# ---------------------------------------------------------------------------


class TestRoutineSkillTrimmed:
    """The point of the slice. If `templates/routine-skill.md`
    re-grows the boilerplate, every install ships ~3KB of duplicated
    bytes per routine. These tests fence that off."""

    def test_routine_skill_references_preamble(self):
        """The trimmed template must point readers at the shared file —
        otherwise a routine's SKILL.md has no idea where the rules
        live."""
        text = ROUTINE_SKILL_TEMPLATE.read_text()
        assert REFERENCE_DEST_PATH in text, (
            f"templates/routine-skill.md must reference "
            f"{REFERENCE_DEST_PATH!r} — without the pointer, a routine "
            f"can't find the shared preamble. (Tests pin the path so "
            f"a typo here breaks loudly.)"
        )

    def test_routine_skill_has_reference_section_header(self):
        """The pointer must live under a clearly-named section so a
        casual reader can find it. `## Reference` is the convention
        SKILL.md line 317 names."""
        text = ROUTINE_SKILL_TEMPLATE.read_text()
        assert re.search(r"^##\s+Reference", text, re.M), (
            "templates/routine-skill.md must have an `## Reference` "
            "section header — SKILL.md install step 6f names this "
            "exact heading as the pointer mechanism"
        )

    def test_routine_skill_keeps_routine_specific_placeholders(self):
        """The trim must not over-shoot — per-routine substitutions
        still need their placeholders in the routine-skill template
        (otherwise renders land with no purpose, no trigger, no
        prompt body)."""
        text = ROUTINE_SKILL_TEMPLATE.read_text()
        for placeholder in (
            "{{routine_id}}",
            "{{purpose}}",
            "{{trigger_summary}}",
            "{{routine_prompt_body}}",
        ):
            assert placeholder in text, (
                f"trimmed routine-skill.md must still have "
                f"{placeholder!r} — without it, rendered files are "
                f"missing the per-routine content the install fills in"
            )

    def test_routine_skill_no_longer_duplicates_full_commit_pr_block(self):
        """Drift detector: the canonical commit/PR procedure now lives
        in the preamble. If someone re-adds the full 30-line block
        back into the per-routine template, this test fails.

        The signal we look for is the routine-skill template
        re-containing the WHOLE 4-step procedure (gh pr create with
        --base and --head) AS WELL AS the routines/ branch checkout.
        A casual mention of `gh pr create` (e.g. in the Reference
        section) is fine — what we're guarding against is the full
        duplicate."""
        text = ROUTINE_SKILL_TEMPLATE.read_text()

        # Bottom line: if BOTH of these are present, the routine-skill
        # template is duplicating the preamble's commit/PR procedure.
        has_branch_checkout = bool(
            re.search(r"git checkout -B routines/", text)
        )
        has_pr_create_full = bool(
            re.search(r"gh pr create[\s\S]*--base[\s\S]*--head", text)
        )
        assert not (has_branch_checkout and has_pr_create_full), (
            "templates/routine-skill.md is duplicating the full "
            "commit/PR procedure from the preamble. Move it to "
            "templates/routine-preamble.md and reference it via "
            "`## Reference`. (This drift detector fired — find and "
            "remove the duplicated block.)"
        )

    def test_routine_skill_no_longer_duplicates_full_log_format(self):
        """Drift detector for the log JSON block. The canonical
        fenced ```json log line lives in the preamble; the per-routine
        template should reference it, not re-embed it."""
        text = ROUTINE_SKILL_TEMPLATE.read_text()
        # Same heuristic as above: a casual mention of `log.jsonl` is
        # fine, but if the template re-contains the full json block
        # with all 6 fields, that's duplication.
        m = re.search(r"```json\s*\n([\s\S]*?)\n```", text)
        if m:
            block = m.group(1).lower()
            field_hits = sum(
                f in block
                for f in ("ts", "routine", "outcome", "summary",
                          "increment_signal", "last_fire_sha")
            )
            assert field_hits < 5, (
                "templates/routine-skill.md re-embeds the full log line "
                "JSON shape from the preamble. Reference the preamble "
                f"instead — found {field_hits}/6 canonical fields in a "
                f"fenced json block."
            )


# ---------------------------------------------------------------------------
# SKILL.md install procedure stays in sync with the file paths
# ---------------------------------------------------------------------------


class TestSkillMdInstallReferences:
    """SKILL.md step 6f tells the install procedure to render the
    preamble. The paths it names MUST match what this slice ships —
    otherwise a fresh install crashes or silently skips the preamble."""

    def test_step_6f_names_template_source_path(self):
        text = SKILL_MD.read_text()
        # The source path the install reads from.
        assert "templates/routine-preamble.md" in text, (
            "SKILL.md must name `templates/routine-preamble.md` as "
            "the source for the shared preamble render — without "
            "this, the install procedure can't locate the file"
        )

    def test_step_6f_names_destination_path(self):
        text = SKILL_MD.read_text()
        assert REFERENCE_DEST_PATH in text, (
            f"SKILL.md must name {REFERENCE_DEST_PATH!r} as the "
            f"render destination — verification at line 380 already "
            f"checks for this path"
        )
