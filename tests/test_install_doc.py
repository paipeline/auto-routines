"""SKILL.md install-flow shape tests (PRD #10 Module 3 / Phase 5).

The install flow described in SKILL.md must reference the preamble file and
its verify checks. Without these tests, SKILL.md silently drifts from the
renderer / sanity-check after a refactor."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "SKILL.md"


def _skill_text() -> str:
    return SKILL_MD.read_text()


def test_skill_md_install_step_renders_preamble() -> None:
    """The install flow must include a step that writes
    `_shared/preamble.md`. Otherwise rendered per-routine SKILLs that
    point at the preamble would dangle."""
    text = _skill_text()
    assert "_shared/preamble.md" in text, (
        "SKILL.md must reference `_shared/preamble.md` so the install "
        "step is documented"
    )
    assert "routine-preamble.md" in text, (
        "SKILL.md must reference the source template `routine-preamble.md`"
    )


def test_skill_md_install_step_mentions_byte_budget() -> None:
    """The byte-budget rule (≤ 3000 bytes) is the load-bearing token-frugality
    guarantee. SKILL.md must spell it out so a Claude doing install knows the
    rendered SKILL must stay slim."""
    text = _skill_text()
    assert "max_routine_skill_bytes" in text or "3000" in text, (
        "SKILL.md must document the byte-budget rule"
    )


def test_skill_md_verify_step_checks_preamble() -> None:
    """Step 7 (verify install) must include a check for the preamble file —
    otherwise an incomplete install passes verify with dangling references."""
    text = _skill_text()
    # Find the Verify section, then check it mentions the preamble.
    verify_idx = text.find("Verify install")
    assert verify_idx > 0, "SKILL.md must have a 'Verify install' section"
    verify_section = text[verify_idx : verify_idx + 4000]
    assert "_shared/preamble.md" in verify_section, (
        "Verify-install step must check `_shared/preamble.md` exists"
    )


def test_skill_md_verify_step_checks_reference_pointer() -> None:
    """Verify must check that rendered per-routine SKILLs contain the
    Reference pointer to the preamble — without it routines have no link to
    the shared boilerplate."""
    text = _skill_text()
    verify_idx = text.find("Verify install")
    verify_section = text[verify_idx : verify_idx + 4000]
    assert "Reference" in verify_section, (
        "Verify-install step must check rendered SKILLs contain the "
        "`## Reference` pointer block"
    )


def test_files_managed_section_lists_shared_preamble() -> None:
    """The 'Files this skill manages' section must list the preamble file
    so users know it exists and where to look."""
    text = _skill_text()
    files_idx = text.find("Files this skill manages")
    assert files_idx > 0
    files_section = text[files_idx : files_idx + 1500]
    assert "_shared/preamble.md" in files_section, (
        "'Files this skill manages' must list `_shared/preamble.md`"
    )
