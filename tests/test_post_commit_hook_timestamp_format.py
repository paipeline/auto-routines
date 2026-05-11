"""
Drift detectors: `templates/post-commit-hook.sh` must use the
canonical timestamp format when writing to `.iteration/log.jsonl`.

Why this matters: the hook is the ONE place that writes log.jsonl
entries in pure shell, not via the orchestrator's Python wrappers.
The rest of the codebase pins "local ISO-8601 with offset, never
UTC `Z`" — checkpoint-append rejects `Z` suffixes, the preamble
canonicalizes the format (`tests/test_routine_preamble.py` pins
this), and the orchestrator's log writes use
`datetime.now().astimezone()`.

If someone "simplifies" the hook to use `date -u +%Y-%m-%dT%H:%M:%SZ`
or `date +%Y-%m-%dT%H:%M:%S` (no offset), log.jsonl ends up with a
mix of timestamp formats and downstream readers (status.py,
dashboard.py, the FSM staleness detector) need to normalize on the
fly — a long-tail source of off-by-N-hours bugs.

What we pin:

  - `date +%Y-%m-%dT%H:%M:%S%z` is the canonical format string.
  - The hook MUST use it for every `ts` it writes.
  - The hook MUST NOT use `-u` (forces UTC), `%Z` (named tz like
    PDT, ambiguous), or a literal `Z` suffix.
  - The hook MUST write `outcome` field (the canonical shape from
    the preamble) so downstream readers can branch on success/err.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HOOK_TEMPLATE = ROOT / "templates" / "post-commit-hook.sh"

CANONICAL_DATE_FMT = "date +%Y-%m-%dT%H:%M:%S%z"


@pytest.fixture(scope="module")
def hook_text() -> str:
    return HOOK_TEMPLATE.read_text()


# ---------------------------------------------------------------------------
# Canonical date format used wherever the hook writes a `ts` field
# ---------------------------------------------------------------------------


class TestHookUsesCanonicalDateFormat:
    """The hook writes log.jsonl entries in pure shell. Every `ts`
    field MUST use `date +%Y-%m-%dT%H:%M:%S%z` — the same format
    the orchestrator emits and `checkpoint-append` enforces."""

    def test_hook_invokes_date_with_canonical_format(self, hook_text):
        assert CANONICAL_DATE_FMT in hook_text, (
            "post-commit-hook.sh must invoke "
            f"`{CANONICAL_DATE_FMT}` — the canonical local ISO "
            "format with offset. Without this, log.jsonl ends up "
            "with mixed timestamp formats across the orchestrator "
            "(local with offset) and the hook (whatever shell "
            "default was used)"
        )

    def test_every_date_invocation_uses_canonical_format(self, hook_text):
        """Catch a half-edit: someone updates one `date` call but
        leaves another with an inconsistent format. Find every
        `date +` invocation (and `date -u`) and pin that they ALL
        match the canonical format string."""
        # Match either `date +<fmt>` or `date -u +<fmt>` — we want
        # to flag both forms so an introduced -u is loud. The format
        # string ends at the first character that closes a `$(...)`
        # subshell (`)`), a quote, or whitespace.
        invocations = re.findall(
            r"date\s+(?:-u\s+)?\+[^\s)\"']+", hook_text
        )
        assert invocations, (
            "no `date +...` invocation found in hook template — "
            "the hook needs at least one to write timestamps. If "
            "the timestamp source was changed (e.g. to a Python "
            "subshell), update this drift detector to match"
        )
        for call in invocations:
            assert call == CANONICAL_DATE_FMT, (
                f"non-canonical date invocation in hook: {call!r}. "
                f"Expected {CANONICAL_DATE_FMT!r}. Mixed timestamp "
                "formats in log.jsonl break downstream readers that "
                "rely on lex-sortable local ISO-with-offset strings"
            )


# ---------------------------------------------------------------------------
# Forbidden timestamp forms: UTC -u, %Z, literal Z suffix
# ---------------------------------------------------------------------------


class TestHookRejectsNonCanonicalTimestampForms:
    """Three concrete failure forms the rest of the codebase
    rejects. Pin them here too so the hook doesn't quietly become
    the one inconsistent writer."""

    def test_no_date_minus_u(self, hook_text):
        """`date -u` forces UTC, breaking the local-with-offset
        invariant. checkpoint-append rejects `Z`-suffixed strings
        precisely to prevent this."""
        assert "date -u" not in hook_text, (
            "post-commit-hook.sh must NOT use `date -u` — that "
            "forces UTC and produces a `Z`-suffixed timestamp "
            "(or an unoffset string), inconsistent with the rest "
            "of the codebase. Use the canonical "
            f"`{CANONICAL_DATE_FMT}` instead"
        )

    def test_no_named_timezone_format(self, hook_text):
        """`%Z` produces a named timezone (PDT, EST) — ambiguous
        and not lex-sortable. `%z` (lowercase) is the canonical
        numeric offset."""
        # Find every `date` invocation and check none uses %Z.
        for m in re.finditer(r"date[^\n]*\+\S+", hook_text):
            assert "%Z" not in m.group(0), (
                f"hook contains `%Z` (named timezone) in date "
                f"invocation: {m.group(0)!r}. Use `%z` (numeric "
                "offset) — the canonical lex-sortable form"
            )

    def test_no_literal_z_suffix_in_timestamps(self, hook_text):
        """Catch a hand-written `Z` suffix in the JSON template
        (`\\"ts\\":\\"...Z\\"`) that bypasses date entirely. This
        is the most direct form of the failure mode."""
        # The JSON shape uses escaped quotes: \"ts\":\"<value>\".
        # Look for a Z just before a closing escaped quote, which
        # would indicate someone hand-coded a UTC suffix.
        offenders = re.findall(r'\\"ts\\":[^\n]*Z\\"', hook_text)
        assert not offenders, (
            f"hook contains literal `Z`-suffixed `ts` value(s): "
            f"{offenders}. checkpoint-append rejects this form; "
            "the hook must not be the one writer that emits it"
        )


# ---------------------------------------------------------------------------
# Canonical JSON shape: outcome field must be present in writes
# ---------------------------------------------------------------------------


class TestHookEmitsCanonicalLogShape:
    """The preamble pins the canonical log.jsonl shape (`ts`,
    `routine`, `outcome`, `summary`, `increment_signal`). The hook
    is a partial writer — it writes routine outcomes (`outcome`) and
    its own errors (`hook` field + `outcome`). The pin here: every
    JSON object the hook writes to log.jsonl carries an `outcome`
    field so downstream readers can branch on success/err."""

    def test_every_hook_log_write_has_outcome_field(self, hook_text):
        """Find every line that appends to LOG (`>> "$LOG"`) and
        check the JSON payload includes `outcome`."""
        # Lines that echo JSON to log.jsonl.
        for m in re.finditer(
            r'echo\s+"[^"\n]*\\"ts\\"[^"\n]*"\s*>>\s*"\$LOG"',
            hook_text,
        ):
            payload = m.group(0)
            assert '\\"outcome\\"' in payload, (
                "post-commit-hook.sh log.jsonl write missing "
                f"`outcome` field: {payload[:120]!r}... — every "
                "log line needs `outcome` so downstream readers "
                "(status.py, dashboard, FSM staleness detector) "
                "can branch on it"
            )
