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
from typing import Any


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
