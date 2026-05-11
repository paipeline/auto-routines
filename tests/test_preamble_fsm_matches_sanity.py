"""
Drift detectors: `templates/routine-preamble.md` documents the FSM
state set and firing-states rule. The canonical state set lives in
`scripts/sanity-check.py::ROUTINE_STATES`; the canonical firing
subset lives there too (`FIRING_STATES`). If these drift, users
read one truth and the orchestrator enforces another.

Concrete failure modes this catches:

  - Someone adds a new state to ROUTINE_STATES (e.g. "PAUSED") but
    forgets the preamble. Routines see an unknown state in their
    config.yaml and don't know whether to fire or skip.

  - Someone removes a state from ROUTINE_STATES (or renames one)
    but the preamble still mentions it. Users see invalid
    documentation; the LLM may try to transition to a state that
    sanity-check immediately rejects.

  - Someone broadens FIRING_STATES (e.g. adds STAGNANT to "fire")
    but the preamble still says "Only ACTIVE and EVOLVING should
    produce work." Routines now fire in STAGNANT state but their
    skill instructions tell them to noop.

  - Someone narrows FIRING_STATES but the preamble doesn't update.
    Inverse problem; same fix.

The cheapest, sturdiest pin is "every canonical state appears in
the preamble's enumerated list" + "preamble's firing-states
sentence matches FIRING_STATES exactly".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from .conftest import sanity


ROOT = Path(__file__).resolve().parent.parent
PREAMBLE_PATH = ROOT / "templates" / "routine-preamble.md"


@pytest.fixture(scope="module")
def preamble_text() -> str:
    return PREAMBLE_PATH.read_text()


def _state_enumeration_line(preamble_text: str) -> str:
    """Return the line that enumerates the canonical state set —
    the line currently reads
    `PROPOSED | ACTIVE | EVOLVING | STAGNANT | COMPLETED | STOPPED`
    (pipe-separated, inside a paragraph that introduces the FSM).
    Bounded to the `## State handling` section so we don't accept
    a casual state mention elsewhere."""
    m = re.search(
        r"^## State handling[^\n]*\n",
        preamble_text,
        re.M,
    )
    assert m, (
        "preamble must have a `## State handling` section — without "
        "it, the FSM is undocumented and this drift detector has "
        "nothing to bind to"
    )
    section = preamble_text[m.end():]
    # The enumeration is the first line containing all the states.
    for line in section.splitlines():
        if "PROPOSED" in line and "ACTIVE" in line and "|" in line:
            return line
    raise AssertionError(
        "could not find the pipe-separated state enumeration line "
        "in `## State handling`. Expected a line like "
        "`PROPOSED | ACTIVE | EVOLVING | STAGNANT | COMPLETED | STOPPED`"
    )


# ---------------------------------------------------------------------------
# Every canonical state appears in the preamble's enumeration
# ---------------------------------------------------------------------------


class TestPreambleEnumeratesEveryCanonicalState:
    """The preamble's `Every routine carries one of:` line must
    enumerate every state in `sanity.ROUTINE_STATES`. Missing a
    state means users don't know it exists; adding a state to
    sanity-check without updating here is the drift this catches."""

    def test_enumeration_covers_every_canonical_state(self, preamble_text):
        line = _state_enumeration_line(preamble_text)
        for state in sanity.ROUTINE_STATES:
            assert state in line, (
                f"preamble state enumeration is missing canonical "
                f"state {state!r}. The line currently reads:\n  "
                f"{line.strip()}\n"
                f"sanity.ROUTINE_STATES is: "
                f"{sorted(sanity.ROUTINE_STATES)}. Add the missing "
                "state or remove it from sanity-check.py"
            )

    def test_enumeration_has_no_phantom_states(self, preamble_text):
        """Inverse: the enumeration must NOT reference states that
        sanity-check doesn't know about. Catches the rename/removal
        case."""
        line = _state_enumeration_line(preamble_text)
        # All ALL-CAPS tokens of length ≥ 4 in the enumeration line
        # — every one of them is claimed to be a state. Any that's
        # not in ROUTINE_STATES is a phantom.
        tokens = re.findall(r"\b[A-Z]{4,}\b", line)
        phantoms = [t for t in tokens if t not in sanity.ROUTINE_STATES]
        assert not phantoms, (
            f"preamble state enumeration references phantom "
            f"state(s) {phantoms} not in sanity.ROUTINE_STATES "
            f"({sorted(sanity.ROUTINE_STATES)}). If these were "
            "renamed/removed in sanity-check.py, update the "
            "preamble line accordingly"
        )


# ---------------------------------------------------------------------------
# Firing-states sentence matches FIRING_STATES exactly
# ---------------------------------------------------------------------------


class TestPreambleFiringStatesMatchesSanity:
    """The preamble says 'Only ACTIVE and EVOLVING should produce
    work.' This sentence must list exactly the states in
    sanity.FIRING_STATES — no more, no fewer."""

    def test_firing_states_sentence_lists_all_firing_states(
        self, preamble_text
    ):
        # Find the sentence that pins the firing rule. Bounded to
        # the `## State handling` section.
        m = re.search(r"^## State handling[^\n]*\n", preamble_text, re.M)
        assert m
        section = preamble_text[m.end():]
        # The canonical sentence reads:
        #   "Only ACTIVE and EVOLVING should produce work."
        # We accept variations on phrasing but pin that the
        # firing-states sentence contains every firing state.
        sentence = None
        for line in section.splitlines():
            if "Only" in line and "produce work" in line:
                sentence = line
                break
        assert sentence is not None, (
            "preamble `## State handling` must have a 'Only X "
            "should produce work' sentence — pins which states "
            "fire vs. noop. The current orchestrator enforces "
            f"FIRING_STATES={sorted(sanity.FIRING_STATES)}"
        )
        for fs in sanity.FIRING_STATES:
            assert fs in sentence, (
                f"firing-states sentence missing {fs!r}. The "
                f"current line is:\n  {sentence.strip()}\n"
                f"sanity.FIRING_STATES is: "
                f"{sorted(sanity.FIRING_STATES)} — every state in "
                "that set must appear in this sentence"
            )

    def test_firing_states_sentence_has_no_phantom_firing_states(
        self, preamble_text
    ):
        """Inverse: the sentence must not claim a NON-firing state
        produces work. Catches the case where someone narrows
        FIRING_STATES but leaves the preamble broad."""
        m = re.search(r"^## State handling[^\n]*\n", preamble_text, re.M)
        section = preamble_text[m.end():]
        sentence = None
        for line in section.splitlines():
            if "Only" in line and "produce work" in line:
                sentence = line
                break
        assert sentence is not None
        # Extract ALL-CAPS tokens of length ≥ 4 from the sentence
        # — these are the claimed firing states. Any that isn't in
        # FIRING_STATES is a phantom.
        tokens = re.findall(r"\b[A-Z]{4,}\b", sentence)
        phantoms = [
            t for t in tokens
            if t in sanity.ROUTINE_STATES
            and t not in sanity.FIRING_STATES
        ]
        assert not phantoms, (
            f"firing-states sentence claims {phantoms} produce "
            f"work, but sanity.FIRING_STATES is "
            f"{sorted(sanity.FIRING_STATES)}. The orchestrator "
            "won't fire routines in those states — fix the "
            "preamble to match"
        )


# ---------------------------------------------------------------------------
# Transition table only references canonical states
# ---------------------------------------------------------------------------


class TestPreambleTransitionTableUsesCanonicalStates:
    """The `| From | To | Owned by |` Markdown table enumerates
    legal transitions. Every state name in the From/To columns
    must be a canonical ROUTINE_STATES member. A typo or rename
    drift in the table would let an LLM "transition" a routine to
    a state the orchestrator rejects."""

    def test_transition_table_states_are_canonical(self, preamble_text):
        # Find the table — bounded to `## State handling`. The
        # table header reads `| From       | To         | Owned by`.
        m = re.search(
            r"^## State handling[^\n]*\n[\s\S]*?\| From\s+\| To\s+\| Owned by",
            preamble_text,
            re.M,
        )
        assert m, (
            "preamble must have a `| From | To | Owned by |` "
            "transition table — the canonical documentation of "
            "what evolve does"
        )
        # Extract just the table rows. The table ends at a blank
        # line.
        after = preamble_text[m.end():]
        rows = []
        for line in after.splitlines():
            if line.strip() == "":
                break
            if line.startswith("|"):
                rows.append(line)
        assert rows, (
            "transition table found but has no body rows — that's "
            "not useful"
        )
        # For each data row (skip the `| --- | --- | --- |` separator),
        # parse the first two columns as state names.
        offenders: list[tuple[str, str]] = []
        for row in rows:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) < 2:
                continue
            frm, to = cells[0], cells[1]
            # Separator row: `---------- | ---------- | ----`
            if set(frm) <= set("- ") or set(to) <= set("- "):
                continue
            # `*` is a legitimate wildcard for the
            # `* -> STOPPED` (terminal) row.
            for label, val in (("From", frm), ("To", to)):
                if val == "*":
                    continue
                if val not in sanity.ROUTINE_STATES:
                    offenders.append((label, val))
        assert not offenders, (
            f"transition table references non-canonical state(s): "
            f"{offenders}. sanity.ROUTINE_STATES is: "
            f"{sorted(sanity.ROUTINE_STATES)}. If a state was "
            "renamed in sanity-check.py, update the table to match"
        )
