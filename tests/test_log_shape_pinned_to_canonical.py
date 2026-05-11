"""
Drift detectors: the `.iteration/log.jsonl` canonical shape documented
in `templates/routine-preamble.md` must match what the readers
(`scripts/status.py`, `scripts/dashboard.py`) actually consume.

Why this matters: log.jsonl is the contract between routine writers
(LLM-side, follow the preamble) and the readers (pure-script,
status/dashboard tools). The preamble's canonical-shape block is
the writer's spec; the readers' `.get("field")` calls are the
reader's spec. If they drift:

  - Reader adds `.get("foo")` for a new field, preamble doesn't
    document it → LLM writers don't produce `foo`, readers see
    `None` and silently miss data.

  - Writer (preamble) drops a field, readers still expect it →
    same silent-None problem.

  - Outcome enum drifts: preamble says `ok|noop|warn|err` but a
    reader checks `e.get("outcome") == "success"` → reader's
    branch never fires.

Two halves pinned here:

  A. Field parity: every field the readers `.get(...)` from a log
     entry must be documented in the preamble's canonical shape.

  B. Outcome enum parity: every literal outcome value used by
     writers (orchestrator, hook) AND readers (status.py) must be
     in the preamble's documented enum.

The hook template is a writer (it writes log.jsonl entries
directly in shell) and was already pinned by
`tests/test_post_commit_hook_timestamp_format.py` for `outcome`
presence; here we pin its literal values match the canonical enum.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PREAMBLE_PATH = ROOT / "templates" / "routine-preamble.md"
STATUS_PATH = ROOT / "scripts" / "status.py"
DASHBOARD_PATH = ROOT / "scripts" / "dashboard.py"
HOOK_PATH = ROOT / "templates" / "post-commit-hook.sh"
ORCHESTRATOR_PATH = ROOT / "scripts" / "orchestrator.py"


@pytest.fixture(scope="module")
def preamble_text() -> str:
    return PREAMBLE_PATH.read_text()


def _canonical_field_set(preamble_text: str) -> set[str]:
    """Extract the field names from the preamble's canonical JSON
    block. The block is fenced (```json ... ```) under the
    `## Outputs — .iteration/log.jsonl` heading."""
    m = re.search(
        r"^## Outputs[^\n]*log\.jsonl[^\n]*\n[\s\S]*?```json\n([\s\S]*?)\n```",
        preamble_text,
        re.M,
    )
    assert m, (
        "preamble must have a fenced ```json``` block under "
        "`## Outputs — .iteration/log.jsonl` that documents the "
        "canonical entry shape — without it the writer contract "
        "is undocumented and this drift detector has nothing to "
        "bind to"
    )
    body = m.group(1)
    # Quoted keys: `"foo":`
    return set(re.findall(r'"([a-z_]+)"\s*:', body))


def _canonical_outcome_set(preamble_text: str) -> set[str]:
    """Extract the documented outcome enum values from the
    preamble's canonical block. The format is
    `"outcome": "ok|noop|warn|err"`. Pipe-separated."""
    m = re.search(
        r'"outcome"\s*:\s*"([a-z|]+)"', preamble_text
    )
    assert m, (
        "preamble canonical block must document `outcome` with a "
        "pipe-separated enum like `ok|noop|warn|err` — without it "
        "the outcome contract is undocumented"
    )
    return set(m.group(1).split("|"))


# ---------------------------------------------------------------------------
# A. Field parity: readers ⊆ preamble canonical fields
# ---------------------------------------------------------------------------


class TestReaderFieldsAreDocumented:
    """Every field a reader (.get("X") on a log entry) consumes
    must appear in the preamble's canonical shape. A new reader
    field that's not documented invites the silent-None drift
    where LLM writers don't produce the field."""

    def _reader_log_fields(self) -> dict[str, set[str]]:
        """Scan status.py and dashboard.py for `.get("name")` calls
        on log entries. Returns {script_path: {field_name, ...}}.

        Heuristic: variables named `e`, `entry`, `last`, `matches`,
        `recent` are commonly log entries in these scripts. We
        match `.get("X")` on those names. False positives are
        better than misses here — extra fields just need to be
        documented or excluded explicitly."""
        log_var_names = ("e", "entry", "last", "row", "ent")
        results: dict[str, set[str]] = {}
        for path in (STATUS_PATH, DASHBOARD_PATH):
            text = path.read_text()
            fields: set[str] = set()
            for var in log_var_names:
                # `var.get("field")` or `var.get("field",`
                for m in re.finditer(
                    rf'\b{var}\.get\("([a-z_]+)"', text
                ):
                    fields.add(m.group(1))
            results[path.name] = fields
        return results

    def test_status_reader_fields_documented_in_preamble(
        self, preamble_text
    ):
        canonical = _canonical_field_set(preamble_text)
        readers = self._reader_log_fields()
        status_fields = readers["status.py"]
        assert status_fields, (
            "status.py reader-fields scan returned empty — heuristic "
            "may be stale (variable-name list out of date). If the "
            "log-reading loop renamed its variable, update "
            "log_var_names in this test"
        )
        undocumented = status_fields - canonical
        assert not undocumented, (
            f"status.py reads log fields {sorted(undocumented)} that "
            "are NOT documented in the preamble's canonical shape "
            f"(documented: {sorted(canonical)}). Either add them to "
            "the preamble or stop reading them — silent-None drift "
            "is what this pin prevents"
        )

    def test_dashboard_reader_fields_documented_in_preamble(
        self, preamble_text
    ):
        canonical = _canonical_field_set(preamble_text)
        readers = self._reader_log_fields()
        dashboard_fields = readers["dashboard.py"]
        assert dashboard_fields, (
            "dashboard.py reader-fields scan returned empty — "
            "heuristic may be stale"
        )
        undocumented = dashboard_fields - canonical
        assert not undocumented, (
            f"dashboard.py reads log fields {sorted(undocumented)} "
            "that are NOT documented in the preamble's canonical "
            f"shape (documented: {sorted(canonical)}). Either add "
            "them to the preamble or stop reading them"
        )


# ---------------------------------------------------------------------------
# B. Outcome enum parity: writer/reader values ⊆ preamble enum
# ---------------------------------------------------------------------------


class TestOutcomeEnumParity:
    """Every literal `outcome` value used in the codebase — by
    writers (orchestrator, hook) and readers (status.py) — must
    be in the preamble's documented enum. A value used in code
    but not in the enum means the preamble is lying to LLMs;
    a value in the enum but used nowhere is dead documentation."""

    def test_orchestrator_outcome_values_in_canonical_enum(
        self, preamble_text
    ):
        """The orchestrator writes literal outcome strings (e.g.
        `"outcome": "ok"`). Every literal must be in the
        preamble's enum."""
        canonical = _canonical_outcome_set(preamble_text)
        text = ORCHESTRATOR_PATH.read_text()
        # `"outcome": "<value>"` or `'outcome': '<value>'`
        values = set(
            re.findall(
                r'["\']outcome["\']\s*:\s*["\']([a-z]+)["\']',
                text,
            )
        )
        unknown = values - canonical
        assert not unknown, (
            f"orchestrator.py writes outcome value(s) {sorted(unknown)} "
            "that are NOT in the preamble's documented enum "
            f"({sorted(canonical)}). Either expand the enum or fix "
            "the orchestrator. Inconsistency here makes LLMs guess"
        )

    def test_hook_outcome_values_in_canonical_enum(self, preamble_text):
        """post-commit-hook.sh writes literal outcome strings via
        echo. Catch any drift the same way."""
        canonical = _canonical_outcome_set(preamble_text)
        text = HOOK_PATH.read_text()
        # The hook uses escaped quotes: \"outcome\":\"<value>\"
        values = set(
            re.findall(
                r'\\"outcome\\"\s*:\s*\\"([a-z]+)\\"',
                text,
            )
        )
        unknown = values - canonical
        assert not unknown, (
            f"post-commit-hook.sh writes outcome value(s) "
            f"{sorted(unknown)} not in the preamble's documented "
            f"enum ({sorted(canonical)})"
        )

    def test_status_reader_outcome_values_in_canonical_enum(
        self, preamble_text
    ):
        """status.py branches on specific outcome values (e.g.
        `e.get("outcome") in {"warn", "err"}` for the noisy
        counter). Every value it branches on must be in the
        preamble's enum — otherwise the branch is unreachable."""
        canonical = _canonical_outcome_set(preamble_text)
        text = STATUS_PATH.read_text()
        # `.get("outcome") in {"warn", "err"}` set-literal
        # comparisons. The `in {...}` keyword separator is required
        # so we don't snag an unrelated dict literal nearby.
        values: set[str] = set()
        for m in re.finditer(
            r'get\(\s*["\']outcome["\']\s*\)\s*in\s*\{([^}]+)\}',
            text,
        ):
            for v in re.findall(r'["\']([a-z]+)["\']', m.group(1)):
                values.add(v)
        # Also catch direct `== "ok"` style comparisons.
        for m in re.finditer(
            r'get\(\s*["\']outcome["\']\s*\)\s*==\s*["\']([a-z]+)["\']',
            text,
        ):
            values.add(m.group(1))
        assert values, (
            "status.py outcome-branch scan returned empty — if the "
            "noisy/useful logic was refactored, update this test's "
            "heuristic"
        )
        unknown = values - canonical
        assert not unknown, (
            f"status.py branches on outcome value(s) {sorted(unknown)} "
            f"not in the preamble's enum ({sorted(canonical)}). The "
            "branch can never fire for an LLM writer that follows "
            "the preamble"
        )


# ---------------------------------------------------------------------------
# Canonical block contains the contract-essential fields
# ---------------------------------------------------------------------------


class TestPreambleCanonicalBlockHasEssentialFields:
    """Sanity check the preamble itself: the canonical shape MUST
    enumerate `ts`, `routine`, `outcome` at minimum. Without
    these three, the readers above have nothing to bind to and
    every downstream test passes vacuously.

    `summary` and `increment_signal` are softer contracts (dashboard
    + status read them respectively); pin them too so the canonical
    shape stays meaningful."""

    @pytest.mark.parametrize(
        "field", ["ts", "routine", "outcome", "summary", "increment_signal"]
    )
    def test_canonical_field_present(self, preamble_text, field):
        canonical = _canonical_field_set(preamble_text)
        assert field in canonical, (
            f"preamble canonical shape is missing {field!r}. The "
            f"current documented field set is: {sorted(canonical)}. "
            "All five of ts/routine/outcome/summary/increment_signal "
            "are consumed by status.py or dashboard.py — dropping "
            "one means readers silently see None"
        )
