"""
Drift detectors: the `automation_level` enum is enforced by
`scripts/sanity-check.py::LEVELS = {"off", "notify", "suggest", "auto"}`,
but its semantics are documented in prose across two independently-
edited surfaces:

  1. `templates/routine-preamble.md` — each level gets a paragraph
     explaining what the routine MUST do at that level (commit + PR,
     write a proposal, log-only, noop).
  2. `templates/config.yaml` — the per-routine `automation_level:`
     field has an inline comment enumerating the legal values
     (`# off | notify | suggest | auto`).

If these drift:

  - Someone adds a new level (e.g. `dry-run`) to LEVELS but forgets
    the preamble. The LLM writer sees `automation_level: dry-run` in
    its config.yaml entry and doesn't know what behavior to take —
    most likely falls back to `auto` and ships changes the user
    wanted held back.

  - Someone removes a level (e.g. drops `notify`) but the preamble
    still has the `If automation_level: notify` paragraph. Users
    read docs that no longer match the enforcer, and a routine
    config that includes the dropped level now fails sanity-check
    after the user already invested in the configuration.

  - The config.yaml comment lies: a level it claims is legal isn't
    in LEVELS, or vice versa. The user pattern-matches off the
    comment and ends up with an unsupported value.

What we pin:

  A. Every `automation_level: <value>` mentioned in the preamble
     (as inline code) must be in `sanity.LEVELS`.
  B. Every value in `sanity.LEVELS` must be documented somewhere
     in the preamble — at minimum referenced as `automation_level:
     <value>`.
  C. The config.yaml comment that enumerates the values must
     exactly cover `sanity.LEVELS`.

Same drift-detection pattern used for FSM states
(`test_preamble_fsm_matches_sanity.py`) and log.jsonl shape
(`test_log_shape_pinned_to_canonical.py`); this slice extends it
to the third independent vocabulary the LLM writer reads.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from .conftest import sanity


ROOT = Path(__file__).resolve().parent.parent
PREAMBLE_PATH = ROOT / "templates" / "routine-preamble.md"
CONFIG_TEMPLATE_PATH = ROOT / "templates" / "config.yaml"


@pytest.fixture(scope="module")
def preamble_text() -> str:
    return PREAMBLE_PATH.read_text()


@pytest.fixture(scope="module")
def config_template_text() -> str:
    return CONFIG_TEMPLATE_PATH.read_text()


def _preamble_level_references(preamble_text: str) -> set[str]:
    """Every `automation_level: <value>` mention in the preamble,
    in any form (inline code or plain text). Returns the set of
    referenced level names.

    The canonical phrasing is `automation_level: <value>` (with
    or without backticks). We accept both because the preamble
    uses backticks for the first reference and plain text in
    subsequent ones."""
    matches = re.findall(
        r"automation_level[:=]\s*`?([a-z][a-z-]*)`?",
        preamble_text,
    )
    return set(matches)


# ---------------------------------------------------------------------------
# A. Preamble has no phantom levels (every mention ⊆ sanity.LEVELS)
# ---------------------------------------------------------------------------


class TestPreambleAutomationLevelsAreCanonical:
    """Every `automation_level: <value>` paragraph in the preamble
    must reference a level the sanity-check enforces. A phantom
    level here means the preamble documents behavior for a level
    that's invalid in config.yaml — users read advice they can't
    act on."""

    def test_preamble_references_only_canonical_levels(self, preamble_text):
        seen = _preamble_level_references(preamble_text)
        assert seen, (
            "preamble has no `automation_level: <value>` references "
            "at all — that's the canonical phrasing for each level's "
            "behavior paragraph. Heuristic may be stale (if the "
            "phrasing changed, update _preamble_level_references) or "
            "the per-level paragraphs were removed entirely (drift)"
        )
        phantoms = seen - sanity.LEVELS
        assert not phantoms, (
            f"preamble documents behavior for level(s) {sorted(phantoms)} "
            f"that are NOT in sanity.LEVELS ({sorted(sanity.LEVELS)}). "
            "Either add them to LEVELS (and the config-comment, see "
            "the other test class) or strip the dead paragraphs from "
            "the preamble — users following the preamble will set a "
            "config value the sanity-check rejects"
        )


# ---------------------------------------------------------------------------
# B. Preamble covers every canonical level (sanity.LEVELS ⊆ preamble)
# ---------------------------------------------------------------------------


class TestPreambleCoversEveryCanonicalLevel:
    """The preamble MUST document each level the sanity-check
    enforces. A canonical level with no preamble paragraph means
    LLM writers see `automation_level: <new-level>` in their
    config and have no documented behavior to fall back on — they
    guess, most often defaulting to `auto`."""

    @pytest.mark.parametrize("level", sorted(sanity.LEVELS))
    def test_canonical_level_documented(self, preamble_text, level):
        seen = _preamble_level_references(preamble_text)
        assert level in seen, (
            f"sanity.LEVELS contains {level!r} but the preamble has "
            f"no `automation_level: {level}` paragraph documenting "
            "what a routine MUST do at that level. The preamble is "
            "the routine writer's spec — without a paragraph here, "
            "the LLM doesn't know how to behave at this level. "
            f"Current preamble references: {sorted(seen)}"
        )


# ---------------------------------------------------------------------------
# C. config.yaml's inline comment matches sanity.LEVELS exactly
# ---------------------------------------------------------------------------


class TestConfigYamlCommentMatchesCanonicalEnum:
    """`templates/config.yaml` carries an inline comment next to
    each `automation_level:` field, e.g. `# off | notify | suggest
    | auto`. The user pattern-matches off this comment when hand-
    editing config.yaml. It MUST exactly cover sanity.LEVELS — no
    more, no fewer."""

    def _comment_levels(self, config_text: str) -> set[str]:
        """Extract the level set from the inline comment. The
        canonical form is:
            automation_level: <value>       # off | notify | suggest | auto
        We scan every line containing both `automation_level:` and a
        `# ... | ... |` comment, and union the pipe-separated
        tokens. Multiple lines may carry the comment (one per
        routine); we accept any of them but require they all agree
        (drift between two routines' comments is itself a defect)."""
        all_sets: list[set[str]] = []
        for line in config_text.splitlines():
            if "automation_level" not in line:
                continue
            if "#" not in line:
                continue
            comment = line.split("#", 1)[1]
            if "|" not in comment:
                continue
            tokens = {
                t.strip() for t in comment.split("|") if t.strip()
            }
            # Only keep tokens that look like enum members (lowercase,
            # ascii). Filter out anything else picked up accidentally.
            tokens = {t for t in tokens if re.fullmatch(r"[a-z][a-z-]*", t)}
            if tokens:
                all_sets.append(tokens)
        if not all_sets:
            return set()
        # All comment-sets must agree. If they don't, return the
        # union and let the assertion below report the drift.
        return set().union(*all_sets)

    def test_config_template_has_comment_enumerating_levels(
        self, config_template_text
    ):
        levels = self._comment_levels(config_template_text)
        assert levels, (
            "templates/config.yaml must have an inline comment on "
            "each `automation_level:` line enumerating the legal "
            "values (e.g. `# off | notify | suggest | auto`). "
            "Without it, users hand-editing the config have nothing "
            "to pattern-match off and will guess values that fail "
            "sanity-check"
        )

    def test_comment_levels_match_sanity_levels(self, config_template_text):
        levels = self._comment_levels(config_template_text)
        missing = sanity.LEVELS - levels
        phantom = levels - sanity.LEVELS
        assert not missing and not phantom, (
            "templates/config.yaml comment enumerating "
            f"`automation_level:` values doesn't match sanity.LEVELS. "
            f"Comment claims: {sorted(levels)}. sanity enforces: "
            f"{sorted(sanity.LEVELS)}. Missing (in enforcer, not in "
            f"comment): {sorted(missing)}. Phantom (in comment, not "
            f"in enforcer): {sorted(phantom)}. Update the comment OR "
            "sanity-check.py — they have to agree"
        )

    def test_every_automation_level_value_in_template_is_canonical(
        self, config_template_text
    ):
        """Beyond the comment, the template's actual default values
        (`automation_level: auto`) must also be in sanity.LEVELS.
        Catches a typo in the template that would ship installs in
        an invalid state. sanity-check would catch this at install
        time, but pinning it here gives a faster signal on a PR."""
        values = set(
            re.findall(
                r"^\s*automation_level:\s*([a-z][a-z-]*)\s*(?:#|$)",
                config_template_text,
                re.M,
            )
        )
        assert values, (
            "templates/config.yaml has no `automation_level: <value>` "
            "default lines — every shipped routine needs one. If the "
            "template was refactored to derive automation_level "
            "elsewhere, update this test"
        )
        unknown = values - sanity.LEVELS
        assert not unknown, (
            f"templates/config.yaml ships default automation_level "
            f"value(s) {sorted(unknown)} not in sanity.LEVELS "
            f"({sorted(sanity.LEVELS)}). Installs from this template "
            "would fail sanity-check immediately"
        )
