#!/usr/bin/env python3
"""
dashboard.py — render the living GitHub issue body that gives the user
their 'what is auto-routines doing right now' view (PRD #10 Module 2).

The renderer is a pure function: state + config + log + now → markdown.
The sync wrapper that calls `gh issue create/edit` is intentionally a
thin shell around it (see sync_dashboard, phase 2). Keeping render pure
means we can unit-test it without ever touching the network.

The user mental model the dashboard supports (PRD #10 user stories
22, 30 — visibility into the work, where to control it):

  - One issue per iteration. Body refreshed on every dispatch.
  - Top: heartbeat (timestamp, event id) — so you can tell it's alive.
  - Status block: kill switch, idle window, GHA cost cap.
  - Routines table: state, surface, trigger, last fire, last outcome.
  - Recent activity: tail of log.jsonl, newest first, capped at 20 lines.
  - Footer: how to control everything (the only edits the user makes).

The DASHBOARD_MARKER constant is embedded in every rendered body so the
sync layer can detect 'is this OUR issue?' and refuse to clobber a
hand-written one.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


# Pinned marker — appears in every rendered dashboard body. Sync layer
# greps for this to find/refuse the dashboard issue. Don't change without
# coordinating a migration; old issues stay detectable across revisions.
DASHBOARD_MARKER = "<!-- auto-routines-dashboard:v1 -->"

# How many log entries to render in the activity tail. Anything more
# bloats the issue body without helping (full history is in log.jsonl).
_RECENT_ACTIVITY_CAP = 20


def _local_iso(now: dt.datetime) -> str:
    """ISO 8601 with ±HHMM offset. Mirrors state.py / routine-skill.md —
    never UTC `Z`."""
    if now.tzinfo is None:
        raise ValueError(
            "render_dashboard requires a tz-aware `now` (e.g. ZoneInfo('UTC')). "
            "Naive datetimes silently default to UTC in stdlib formatters; "
            "PRD #10 review specifically banned that footgun."
        )
    return now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _format_dispatch_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    # Trim seconds for readability: 2026-05-10T16:30:00-0700 → 2026-05-10 16:30 -0700
    try:
        # Don't be heroic about parsing — just slice if it matches the
        # expected shape. If it doesn't, render as-is so we never crash.
        if "T" in ts and len(ts) >= 19:
            date, rest = ts.split("T", 1)
            time_part = rest[:5]   # HH:MM
            offset = rest[8:] if len(rest) >= 8 else ""
            return f"{date} {time_part} {offset}".strip()
    except Exception:  # pragma: no cover — defensive
        pass
    return ts


def _status_block(state: dict, meta: dict) -> list[str]:
    lines: list[str] = ["## Status", ""]

    # Kill switch
    config_kill = bool(meta.get("kill_switch", False))
    state_kill = bool(state.get("kill_switch_active", False))
    if config_kill or state_kill:
        lines.append("- **Kill switch: ⚠️ ACTIVE** — no routines will dispatch.")
        if config_kill and not state_kill:
            lines.append("  - source: `config.meta.kill_switch`")
        elif state_kill and not config_kill:
            lines.append("  - source: `state.kill_switch_active`")
        else:
            lines.append("  - source: both `config.meta.kill_switch` and `state.kill_switch_active`")
    else:
        lines.append("- Kill switch: 🟢 inactive")

    # Idle window
    idle_window = meta.get("idle_window", "always")
    idle_tz = meta.get("idle_window_tz", "")
    if idle_window == "always":
        lines.append("- Idle window: **disabled** (always firing — no idle hours configured)")
    else:
        lines.append(f"- Idle window: `{idle_window}` ({idle_tz})")

    # GHA cost cap
    used = state.get("gha_minutes_used_today", 0)
    cap = meta.get("gha_minutes_cap", 60)
    reset_date = state.get("gha_minutes_reset_date", "?")
    pct = (used * 100 // cap) if cap else 0
    lines.append(
        f"- GHA cost: **{used} / {cap} min** today ({pct}%) — "
        f"resets at midnight {idle_tz or 'UTC'} (next: after `{reset_date}`)"
    )
    return lines


def _routines_table(routines: list[dict], last_dispatch: dict[str, dict]) -> list[str]:
    lines: list[str] = [
        "## Routines",
        "",
        "| ID | State | Surface | Trigger | Last fire | Last outcome |",
        "|---|---|---|---|---|---|",
    ]
    for r in routines:
        rid = r.get("id", "?")
        rstate = r.get("state", "?")
        # Surface: hook/git-hook/loop run in-session; pull from
        # execution_surface for scheduled/pr-poll.
        prim = r.get("primitive", "?")
        if prim in ("hook", "git-hook", "loop"):
            surface = f"local ({prim})"
        else:
            surface = r.get("execution_surface", "?")
        human = (r.get("trigger") or {}).get("human") or prim
        last = last_dispatch.get(rid)
        if last:
            ts = _format_dispatch_ts(last.get("ts"))
            outcome = last.get("outcome", "?")
        else:
            ts = "—"
            outcome = "—"
        lines.append(
            f"| `{rid}` | {rstate} | {surface} | {human} | {ts} | {outcome} |"
        )
    return lines


def _activity_block(log_entries: list[dict]) -> list[str]:
    lines: list[str] = ["## Recent activity", ""]
    if not log_entries:
        lines.append("_(no log entries yet)_")
        return lines
    # Newest first. We assume `ts` strings are ISO 8601 with offset, so
    # lex-sort works for ordering within a single tz; but log.jsonl is
    # appended in real time so reversing is the safer cross-tz move.
    recent = list(reversed(log_entries))[:_RECENT_ACTIVITY_CAP]
    for entry in recent:
        ts = _format_dispatch_ts(entry.get("ts"))
        routine = entry.get("routine", "?")
        outcome = entry.get("outcome", "?")
        summary = entry.get("summary") or "(no summary)"
        lines.append(f"- `{ts}` — **{routine}** ({outcome}): {summary}")
    return lines


def _footer_block() -> list[str]:
    return [
        "## How to control this",
        "",
        "All dials live in `.iteration/config.yaml` — edit and commit, "
        "the next tick picks up the change.",
        "",
        "- **Pause everything**: set `meta.kill_switch: true`",
        "- **Pause one routine**: set its `state: STOPPED`",
        "- **Change idle window**: edit `meta.idle_window` and `meta.idle_window_tz`",
        "- **Raise/lower GHA budget**: edit `meta.gha_minutes_cap`",
        "- **Run a routine right now**: `/run <routine-id>` from the auto-routines skill",
        "",
        "---",
        "🤖 Auto-managed by auto-routines. **Do not edit this issue body** — "
        "it is overwritten on every dispatch.",
    ]


def render_dashboard(
    state: dict,
    config: dict,
    log_entries: list[dict],
    *,
    now: dt.datetime,
) -> str:
    """Return the markdown body for the living dashboard issue.

    Pure function — never mutates inputs, never does I/O.

    `now` must be tz-aware; naive datetimes raise ValueError (silent UTC
    interpretation was the bug PRD #10 review called out)."""
    if now.tzinfo is None:
        raise ValueError(
            "render_dashboard requires a tz-aware `now`. Pass with tzinfo "
            "(e.g. ZoneInfo('UTC') or ZoneInfo('America/Los_Angeles'))."
        )

    meta = config.get("meta", {}) or {}
    routines = config.get("routines", []) or []
    last_dispatch = state.get("last_dispatch", {}) or {}
    iter_n = config.get("last_iter", 0)
    event_id = state.get("last_event_id", 0)

    title = f"# auto-routines dashboard — iter {iter_n}"
    heartbeat = (
        f"_Last updated: {_local_iso(now)} · "
        f"event #{event_id}_"
    )

    chunks: list[list[str]] = [
        [title, "", heartbeat, ""],
        _status_block(state, meta),
        [""],
        _routines_table(routines, last_dispatch),
        [""],
        _activity_block(log_entries),
        [""],
        _footer_block(),
        ["", DASHBOARD_MARKER, ""],
    ]
    return "\n".join(line for chunk in chunks for line in chunk)


# ---------------------------------------------------------------------------
# sync_dashboard — wrap the renderer in `gh issue create/edit`
# ---------------------------------------------------------------------------

# The label gh-issue list returns when a title-search returns nothing —
# we only ever look for the marker in the body, so title is incidental.
_ISSUE_LIST_LIMIT = 200  # cap to keep `gh issue list` snappy on busy repos


def default_gh_run(args: list[str]) -> str:
    """Default `gh_run`: shell out to the `gh` CLI and return stdout.

    Raises CalledProcessError on nonzero exit so callers can catch
    auth / rate-limit failures explicitly. Prefix `gh` is implicit —
    callers pass ['issue', 'list', '--repo', ...]."""
    completed = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _find_dashboard_issue(
    *,
    repo: str,
    gh_run: Callable[[list[str]], str],
) -> dict | None:
    """Return the existing dashboard issue (matching DASHBOARD_MARKER)
    or None. Searches OPEN + CLOSED so we don't double-create after a
    user closes the issue — we update the existing closed one in place."""
    out = gh_run([
        "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", str(_ISSUE_LIST_LIMIT),
        "--json", "number,title,url,body",
    ])
    try:
        issues = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return None
    for issue in issues:
        body = issue.get("body") or ""
        if DASHBOARD_MARKER in body:
            return issue
    return None


def _issue_number_from_url(url: str) -> int | None:
    m = re.search(r"/issues/(\d+)", url or "")
    return int(m.group(1)) if m else None


def sync_dashboard(
    body: str,
    *,
    repo: str,
    iter_n: int,
    gh_run: Callable[[list[str]], str] | None = None,
) -> dict:
    """Push `body` to the living dashboard issue. Returns:

        {action: "created"|"updated"|"unchanged",
         issue_url: str|None,
         issue_number: int|None}

    Refuses to sync a body that doesn't contain DASHBOARD_MARKER (would
    be unfindable on the next tick). Refuses empty `repo`.

    Existing-issue resolution: looks for any open OR closed issue whose
    BODY contains DASHBOARD_MARKER. Title is irrelevant — that's how we
    avoid clobbering a hand-written issue that happens to have a similar
    name.
    """
    if not body or DASHBOARD_MARKER not in body:
        raise ValueError(
            "sync_dashboard refuses to write a body without the dashboard "
            f"marker {DASHBOARD_MARKER!r}. Use render_dashboard() to build it."
        )
    if not repo:
        raise ValueError("sync_dashboard requires a non-empty repo (owner/name)")
    if gh_run is None:
        gh_run = default_gh_run

    existing = _find_dashboard_issue(repo=repo, gh_run=gh_run)

    if existing is None:
        # Create — write body to a tempfile and pass --body-file so we
        # don't have to worry about argv length limits or shell quoting.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write(body)
            body_path = f.name
        try:
            url_out = gh_run([
                "issue", "create",
                "--repo", repo,
                "--title", f"auto-routines dashboard — iter {iter_n}",
                "--body-file", body_path,
            ])
        finally:
            try:
                Path(body_path).unlink()
            except OSError:
                pass
        url = (url_out or "").strip().splitlines()[-1] if url_out.strip() else None
        return {
            "action": "created",
            "issue_url": url,
            "issue_number": _issue_number_from_url(url or ""),
        }

    # Existing dashboard found.
    existing_body = existing.get("body") or ""
    number = existing.get("number")
    url = existing.get("url")
    if existing_body == body:
        # Save the user a notification — don't churn the timestamp.
        return {
            "action": "unchanged",
            "issue_url": url,
            "issue_number": number,
        }

    # Update. Pass body via tempfile for the same reason as create.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(body)
        body_path = f.name
    try:
        gh_run([
            "issue", "edit", str(number),
            "--repo", repo,
            "--body-file", body_path,
        ])
    finally:
        try:
            Path(body_path).unlink()
        except OSError:
            pass

    return {
        "action": "updated",
        "issue_url": url,
        "issue_number": number,
    }
