#!/usr/bin/env python3
"""
local_poller.py — pulls local-surface dispatch requests off the
`.iteration/local_dispatches.jsonl` event log and (in a follow-up PR)
fans them out to the user's local Claude Code via subprocess.

Architecture (PRD #10 OQ4 resolution):
======================================

GitHub's `repository_dispatch` API is **write-only** — there's no
endpoint to list past dispatches. So the original Module 4 plan of
"workflow emits dispatches, local hook consumes them" had no actual
consumer surface. We replace it with a tiny event-sourcing pattern:

    .github/workflows/auto-routines.yml
        ↓ (orchestrator decides "fire local")
        appends one JSON object per fire to .iteration/local_dispatches.jsonl
        ↓ (commit-back step, same as state.json)
        pushes to main
        ↓
    local poller (this file, run from a Stop hook or cron)
        git fetch origin main
        read .iteration/local_dispatches.jsonl from origin/main
        filter to entries with event_id > local watermark
        fan out to subprocess `claude --skill <routine_id>`
        write new watermark

Watermark is per-clone (lives in .iteration/.poller-watermark, gitignored).
The append-only log is committed (so any clone can replay), but each
clone tracks its own consumption point.

Why an append-only JSONL and not state.json?
--------------------------------------------
state.json's `last_dispatch` field is a "what happened last for each
routine" snapshot — needed by the dashboard + sanity checks. Mixing in
a queue would mean dual semantics and the validator gets uglier. A
separate file for events is cleaner; state.json stays a snapshot.

Public surface
==============

  parse_log_lines(lines: Iterable[str]) -> list[FireRequest]
      Pure: JSONL strings → validated dicts.
      Raises ValueError with a line number on malformed input.

  filter_new(entries, watermark) -> list[FireRequest]
      Pure: drop entries with event_id <= watermark.

  max_event_id(entries, current) -> int
      Pure: compute the new watermark, never regressing below `current`.

  cli_main(argv, *, stdout=None, stderr=None) -> int
      argparse entry point. Subcommands: `scan` (dry-run for now;
      subprocess fan-out lands in a follow-up).

The pure functions are dep-free so the orchestrator could import them
without dragging in argparse / subprocess. The CLI shim is the integration
layer.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Iterable


# Same kebab-case regex as state.py / sanity-check.py. Duplicated so this
# module has zero cross-imports — the orchestrator imports both.
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# Local-time ISO 8601 with explicit ±HHMM offset. UTC `Z` is banned
# (logs are read on the user's machine; their wallclock is the truth).
_LOCAL_ISO_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$"
)

REQUIRED_FIELDS = ("event_id", "routine_id", "ts", "sha")


def _is_strict_int(v: Any) -> bool:
    """isinstance(True, int) is True. Reject bools so the watermark
    comparison can never silently misorder entries."""
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def parse_log_lines(lines: Iterable[str]) -> list[dict]:
    """Parse a JSONL stream into a list of validated FireRequest dicts.

    Pure: no I/O, no logging. Returns the dicts in input order. Raises
    ValueError with a 1-based line number on the first malformed entry —
    we want fail-loud here because silently dropping fires would lose the
    user's work without warning."""
    out: list[dict] = []
    for i, raw in enumerate(lines, start=1):
        if raw is None:
            continue
        s = raw.strip()
        if not s:
            continue  # blank / trailing-newline lines are normal
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"local_dispatches.jsonl line {i}: not valid JSON ({e.msg})"
            ) from e
        if not isinstance(obj, dict):
            raise ValueError(
                f"local_dispatches.jsonl line {i}: must be a JSON object, "
                f"got {type(obj).__name__}"
            )
        for k in REQUIRED_FIELDS:
            if k not in obj:
                raise ValueError(
                    f"local_dispatches.jsonl line {i}: missing field {k!r}"
                )
        if not _is_strict_int(obj["event_id"]) or obj["event_id"] < 0:
            raise ValueError(
                f"local_dispatches.jsonl line {i}: event_id must be a "
                f"non-negative int, got {obj['event_id']!r}"
            )
        rid = obj["routine_id"]
        if not isinstance(rid, str) or not _KEBAB.match(rid):
            raise ValueError(
                f"local_dispatches.jsonl line {i}: routine_id must be "
                f"kebab-case, got {rid!r}"
            )
        ts = obj["ts"]
        if not isinstance(ts, str) or not _LOCAL_ISO_TS.match(ts):
            raise ValueError(
                f"local_dispatches.jsonl line {i}: ts must be local ISO "
                f"8601 with ±HHMM offset (no UTC 'Z'), got {ts!r}"
            )
        if not isinstance(obj["sha"], str) or not obj["sha"]:
            raise ValueError(
                f"local_dispatches.jsonl line {i}: sha must be a non-empty "
                f"string, got {obj['sha']!r}"
            )
        out.append(obj)
    return out


def filter_new(entries: list[dict], watermark: int) -> list[dict]:
    """Return entries with event_id strictly greater than `watermark`.

    Order-preserving; no sorting or deduplication (a duplicate event_id
    is a workflow bug worth surfacing, not silently swallowing)."""
    return [e for e in entries if e["event_id"] > watermark]


def max_event_id(entries: list[dict], current: int) -> int:
    """Return the new watermark — max(event_ids ∪ {current}). Never
    regresses below `current` (a partial fetch from origin shouldn't
    rewind progress)."""
    if not entries:
        return current
    return max(current, max(e["event_id"] for e in entries))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_log_file(path: str) -> list[str]:
    """Read a log file as a list of lines. Missing file → []."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="local_poller",
        description=(
            "Poll .iteration/local_dispatches.jsonl for routine fires "
            "queued by the GHA workflow and fan them out to local "
            "Claude Code subprocesses."
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    scan = sub.add_parser(
        "scan",
        help="Read the dispatch log and report pending fires (dry-run "
        "in this PR; subprocess fan-out lands in a follow-up).",
    )
    scan.add_argument(
        "--log", required=True,
        help="Path to .iteration/local_dispatches.jsonl",
    )
    scan.add_argument(
        "--watermark", type=int, default=0,
        help="Last event_id this poller already consumed (default: 0).",
    )
    scan.add_argument(
        "--dry-run", action="store_true",
        help="Emit pending fires as JSON to stdout; do nothing else. "
        "Currently the only mode — non-dry-run lands in a follow-up PR.",
    )

    return p


def cli_main(argv: list[str], *, stdout=None, stderr=None) -> int:
    """Argparse-driven entry point. Returns an int exit code so __main__
    can sys.exit on it. stdout/stderr are injectable for in-process
    testing without subprocess overhead."""
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr

    parser = _make_parser()
    # Argparse calls sys.exit on --help / errors. Catch + return so
    # callers (tests) get a clean int back.
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0

    if args.cmd is None:
        parser.print_help(stderr)
        return 2

    if args.cmd == "scan":
        return _cmd_scan(args, stdout, stderr)

    print(f"unknown subcommand: {args.cmd}", file=stderr)
    return 2


def _cmd_scan(args, stdout, stderr) -> int:
    lines = _read_log_file(args.log)
    try:
        entries = parse_log_lines(lines)
    except ValueError as e:
        print(f"error parsing dispatch log: {e}", file=stderr)
        return 1

    pending = filter_new(entries, watermark=args.watermark)
    next_watermark = max_event_id(entries, current=args.watermark)

    payload = {
        "pending": pending,
        "next_watermark": next_watermark,
        "log_path": args.log,
    }
    json.dump(payload, stdout, indent=2)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
