"""
Tests for `orchestrator.py checkpoint-append`.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Add tests for the `evolve` flow — drain evolve_requests.jsonl,
    perform the FSM transitions, write a checkpoint, apply, verify."

Drain shipped in Tick 29 (`TestDrainEvolveRequests`). Deterministic
FSM transitions shipped in Tick 30 (`TestFsmPlan`). This slice ships
the **checkpoint write** half — the pure-data portion of SKILL.md
`Mode: evolve` step 7 ("Checkpoint, two-step amend pattern as in
init").

Why a wrapper at all? The LLM keeps fat-fingering two things when
asked to write a checkpoint line:
  - The iter number (off-by-one against existing rows in
    `.iteration/checkpoints.md`)
  - The timestamp (UTC `Z` instead of local with offset, despite
    SKILL.md saying "never UTC `Z` — logs are read on local
    machines")

Both are mechanical: parse existing rows, take max+1, render local
ISO timestamp. Pull them into a pure-script subcommand and the LLM
just calls it.

Format choice: the existing `.iteration/checkpoints.md` in this repo
uses a Markdown table (`| iter | when | sha | summary |`), which is
more useful than the SKILL.md prose template (`iter-NNN: <sha>  <ts>`)
— it has a summary column humans actually read. This wrapper produces
the table format. A separate slice will harmonize the SKILL.md
install template (step 6k) to call the wrapper.

The actual git commit / `commit --amend` dance stays in the SKILL.md
prose — that's the side-effectful half. This wrapper only touches
the data file.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location(
        "orchestrator", ROOT / "scripts" / "orchestrator.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return _load_orchestrator()


# Canonical table header lines the wrapper writes when initializing
# a fresh checkpoints.md. These match the existing in-repo file's
# header so a freshly-initialized repo and an actively-used repo
# share the same shape.
TABLE_HEADER_RE = re.compile(r"^\|\s*iter\s*\|\s*when\s*\|\s*sha\s*\|\s*summary\s*\|", re.M)
SEPARATOR_RE = re.compile(r"^\|[-\s|]+\|", re.M)


# ---------------------------------------------------------------------------
# Fresh-file path: first checkpoint ever
# ---------------------------------------------------------------------------


class TestFirstCheckpoint:
    def test_writes_table_header_when_file_missing(self, orch, tmp_path):
        """The wrapper must initialize a fresh checkpoints.md with
        the canonical header — otherwise an empty install has no
        header row, and subsequent reads (status command,
        dashboards) can't tell where the table starts."""
        f = tmp_path / "checkpoints.md"
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "abc1234",
                "--summary", "install auto-routines",
            ],
            stdout=out,
        )
        assert rc == 0, out.getvalue()
        text = f.read_text()
        assert TABLE_HEADER_RE.search(text), (
            f"checkpoints.md must start with a Markdown table header "
            f"after the first append; got:\n{text}"
        )
        assert SEPARATOR_RE.search(text), (
            "checkpoints.md must include the Markdown table separator "
            "row (|---|---|...) under the header"
        )

    def test_first_iter_number_is_1(self, orch, tmp_path):
        """A fresh install's first checkpoint is `iter 1`. Off-by-zero
        here would shift every subsequent iter number, breaking
        downstream cross-references (PR bodies, log lines, history
        files all reference iter-001)."""
        f = tmp_path / "checkpoints.md"
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "abc1234",
                "--summary", "first iter",
            ],
            stdout=out,
        )
        assert rc == 0
        text = f.read_text()
        # The row should be `| 1 | <ts> | abc1234 | first iter |`.
        # Tolerate any whitespace inside the cells.
        assert re.search(r"^\|\s*1\s*\|.*\|\s*abc1234\s*\|.*\|", text, re.M), (
            f"first checkpoint row must be `iter = 1`; got:\n{text}"
        )

    def test_emits_appended_row_to_stdout(self, orch, tmp_path):
        """The caller (the LLM in Mode: evolve, or a script that
        wraps this) reads stdout to know what got written. Without
        echoing the row, the caller has to re-read the file — which
        risks racing other writers."""
        f = tmp_path / "checkpoints.md"
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "deadbee",
                "--summary", "test",
            ],
            stdout=out,
        )
        assert rc == 0
        captured = out.getvalue()
        assert "deadbee" in captured, (
            "wrapper must echo the appended row on stdout so the "
            "caller can log it without re-reading the file"
        )
        assert "test" in captured


# ---------------------------------------------------------------------------
# Append path: existing file with rows
# ---------------------------------------------------------------------------


class TestSubsequentAppend:
    def _seed_existing_file(self, path: Path, max_iter: int = 2):
        """Write a canonical checkpoints.md with rows for iters
        1..max_iter so we can pin the appending behavior."""
        lines = [
            "# auto-routines checkpoints",
            "",
            "Each row is a successful iter checkpoint. Append-only.",
            "",
            "| iter | when | sha | summary |",
            "|------|------|-----|---------|",
        ]
        for n in range(1, max_iter + 1):
            lines.append(
                f"| {n} | 2026-05-09T19:30:09+0200 | sha{n:04d} | seeded iter {n} |"
            )
        path.write_text("\n".join(lines) + "\n")

    def test_iter_number_is_max_plus_one(self, orch, tmp_path):
        """The next iter number is `max(existing) + 1`. Using `count`
        instead of `max` would silently skip past a corrupt/missing
        row (or worse, re-use an existing number). Use max for
        idempotency under partial-write recovery."""
        f = tmp_path / "checkpoints.md"
        self._seed_existing_file(f, max_iter=5)
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "1111111",
                "--summary", "sixth iter",
            ],
            stdout=out,
        )
        assert rc == 0
        text = f.read_text()
        assert re.search(r"^\|\s*6\s*\|.*\|\s*1111111\s*\|", text, re.M), (
            f"next iter after `max=5` must be 6; got:\n{text}"
        )

    def test_preserves_existing_rows(self, orch, tmp_path):
        """Append-only — never rewrite. If the wrapper drops the
        existing rows, history is lost and `revert iter-NNN` (per
        SKILL.md `Mode: revert`) can't find its SHA."""
        f = tmp_path / "checkpoints.md"
        self._seed_existing_file(f, max_iter=3)
        before = f.read_text()
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "9999999",
                "--summary", "added row",
            ],
            stdout=io.StringIO(),
        )
        assert rc == 0
        after = f.read_text()
        # Every line that was there before must still be there.
        for line in before.splitlines():
            if not line.strip():
                continue
            assert line in after, (
                f"checkpoints.md must be append-only; line {line!r} "
                f"dropped after append"
            )

    def test_does_not_re_emit_header_on_existing_file(self, orch, tmp_path):
        """If the table header is already present, don't write it
        again — duplicate headers break Markdown table rendering
        and confuse downstream parsers."""
        f = tmp_path / "checkpoints.md"
        self._seed_existing_file(f, max_iter=1)
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "abcdef0",
                "--summary", "second",
            ],
            stdout=io.StringIO(),
        )
        assert rc == 0
        text = f.read_text()
        # The header row should appear exactly once.
        assert text.count("| iter | when | sha | summary |") == 1, (
            f"checkpoints.md must contain the table header exactly "
            f"once even after appending; got:\n{text}"
        )


# ---------------------------------------------------------------------------
# Row format — the contract downstream readers depend on
# ---------------------------------------------------------------------------


class TestRowFormat:
    def test_row_is_pipe_delimited_with_4_cells(self, orch, tmp_path):
        f = tmp_path / "checkpoints.md"
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "cafebab",
                "--summary", "shape check",
            ],
            stdout=out,
        )
        assert rc == 0
        text = f.read_text()
        # Find the appended row (skip header + separator).
        data_rows = [
            ln for ln in text.splitlines()
            if ln.startswith("|") and "cafebab" in ln
        ]
        assert len(data_rows) == 1, (
            f"expected exactly one data row containing the SHA; "
            f"got: {data_rows}"
        )
        row = data_rows[0]
        # 4 cells → 5 pipe characters (one at each boundary plus end).
        pipe_count = row.count("|")
        assert pipe_count == 5, (
            f"row must have 4 pipe-delimited cells (iter|ts|sha|summary), "
            f"so 5 pipes; got {pipe_count} in: {row}"
        )

    def test_timestamp_is_local_iso_with_offset_not_z(self, orch, tmp_path):
        """SKILL.md is explicit: "never UTC `Z`". The local user reads
        these on their machine; UTC requires mental arithmetic. The
        wrapper must use `strftime('%Y-%m-%dT%H:%M:%S%z')` (which
        produces `+HHMM` or `-HHMM`, never `Z`)."""
        f = tmp_path / "checkpoints.md"
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "abc1234",
                "--summary", "tz check",
            ],
            stdout=io.StringIO(),
        )
        assert rc == 0
        text = f.read_text()
        # Find the timestamp cell. The row is:
        # | <iter> | <ts> | <sha> | <summary> |
        data_rows = [ln for ln in text.splitlines() if "abc1234" in ln]
        assert data_rows
        cells = [c.strip() for c in data_rows[0].split("|")]
        # cells[0] is "" (leading pipe), cells[1]=iter, cells[2]=ts, ...
        ts_cell = cells[2]
        assert "Z" not in ts_cell, (
            f"timestamp must NOT use UTC `Z`; got {ts_cell!r}. "
            f"SKILL.md is explicit about local-offset ISO."
        )
        # Must look like an ISO-8601 with an offset.
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2}$",
            ts_cell,
        ), (
            f"timestamp must be ISO-8601 with offset (e.g. "
            f"2026-05-11T17:03:00-0700 or +02:00); got {ts_cell!r}"
        )


# ---------------------------------------------------------------------------
# Argparse contract + error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_requires_sha_argument(self, orch, tmp_path):
        """A checkpoint without a SHA is meaningless — `revert
        iter-NNN` looks up the SHA to reset to. Argparse must reject
        a missing --sha so we never write a row with no SHA cell."""
        f = tmp_path / "checkpoints.md"
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--summary", "no sha",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0, (
            "argparse must reject missing --sha — a checkpoint row "
            "without a SHA can't be reverted to"
        )

    def test_requires_summary_argument(self, orch, tmp_path):
        """Same intent — a summary-less row leaves the human-facing
        cell empty. SKILL.md `Mode: revert` shows the summary cell
        to the user when picking which iter to revert to; empty
        summary means a guessing game."""
        f = tmp_path / "checkpoints.md"
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "abc",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0

    def test_summary_with_pipe_char_is_rejected(self, orch, tmp_path):
        """A literal `|` in the summary would split a single cell
        into two and break the Markdown table. The wrapper must
        reject this loudly rather than silently corrupt the table.
        (Future enhancement: escape with `\\|`; for now reject.)"""
        f = tmp_path / "checkpoints.md"
        rc = orch.cli_main(
            [
                "checkpoint-append",
                "--file", str(f),
                "--sha", "abc",
                "--summary", "broke | the table",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0, (
            "summary with a literal `|` must be rejected — silently "
            "writing it would break the Markdown table"
        )
