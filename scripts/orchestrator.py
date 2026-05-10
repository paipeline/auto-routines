#!/usr/bin/env python3
"""
orchestrator.py — the deep module behind PRD #10 Module 1.

Composes a single pure decision per tick:

    tick(trigger, state, config) -> DispatchDecision

`tick()` itself will be added in phase 2. This file currently exposes the
small pure helpers it composes — keeping them here (and not in
sanity-check.py) keeps the validator's surface tight while still letting
the orchestrator's tests assert each rule in isolation.

Design rules:
- No I/O, no `datetime.now()`, no globals. Callers always pass `now`.
- All datetime inputs are tz-aware. We refuse naive datetimes loudly.
- `idle_window_tz` is the source of truth for clock comparisons —
  PRD #10 review specifically called out 'silent UTC fallback' as a
  footgun, so we always convert.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Mirrors sanity.FIRING_STATES — duplicated here so the orchestrator
# has zero imports from the rest of the repo (the test pins both).
FIRING_STATES: frozenset = frozenset({"ACTIVE", "EVOLVING"})

_IDLE_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def _to_zone(now: dt.datetime, tz_name: str) -> dt.datetime:
    """Convert a tz-aware datetime to `tz_name`. Refuse naive datetimes
    loudly — silent UTC interpretation was the bug."""
    if now.tzinfo is None:
        raise ValueError(
            "orchestrator helpers require tz-aware datetimes "
            "(pass `now` with a tzinfo, e.g. ZoneInfo('UTC'))"
        )
    try:
        target = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"invalid IANA timezone: {tz_name!r}") from e
    return now.astimezone(target)


def is_in_idle_window(now: dt.datetime, idle_window: str, tz_name: str) -> bool:
    """True iff `now` (converted to `tz_name`) falls inside `idle_window`.

    `idle_window` is either:
      - "always"     — schema-v4 opt-out (always returns False — never idle)
      - "HH:MM-HH:MM" — clock range; END is exclusive; range may wrap midnight

    We deliberately treat the END as exclusive so adjacent windows
    (e.g. one routine 09:00-13:00, another 13:00-17:00) don't both fire
    at 13:00 sharp. Same convention as cron-style schedule boundaries.
    """
    if idle_window == "always":
        return False
    if not _IDLE_WINDOW_RE.match(idle_window):
        raise ValueError(
            f"idle_window must be 'always' or 'HH:MM-HH:MM', got {idle_window!r}"
        )
    local = _to_zone(now, tz_name)
    start_str, end_str = idle_window.split("-")
    sh, sm = (int(x) for x in start_str.split(":"))
    eh, em = (int(x) for x in end_str.split(":"))
    cur_minutes = local.hour * 60 + local.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    if start_minutes < end_minutes:
        # Normal range, e.g. 09:00-17:00
        return start_minutes <= cur_minutes < end_minutes
    if start_minutes > end_minutes:
        # Wraps midnight, e.g. 22:00-08:00 → in if >=22:00 OR <08:00
        return cur_minutes >= start_minutes or cur_minutes < end_minutes
    # start == end — degenerate, treat as never-idle (zero-length window)
    return False


def should_reset_cost(
    now: dt.datetime, reset_date: str, tz_name: str
) -> bool:
    """True iff today (in `tz_name`) is strictly after `reset_date`.

    `reset_date` is the ISO date the daily GHA-minute counter was last
    rolled. A tick that lands on the next local day rolls the counter
    back to zero (caller's responsibility — this helper only signals).

    Returns False on clock skew (now < reset_date) so we don't zero an
    in-progress window."""
    try:
        stored = dt.date.fromisoformat(reset_date)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"reset_date must be ISO 'YYYY-MM-DD', got {reset_date!r}"
        ) from e
    today = _to_zone(now, tz_name).date()
    return today > stored


def would_exceed_cap(used: int, est: int, cap: int) -> bool:
    """True iff dispatching a routine of `est` minutes would push the
    daily total past `cap`. Used by the orchestrator before each GHA fire.

    Comparison is `used + est > cap` so a routine that EXACTLY fills the
    remaining budget still fires (it just leaves zero headroom)."""
    return (used + est) > cap


def is_firing_state(state: Any) -> bool:
    """True iff a routine in this FSM state is allowed to dispatch.

    Defensive: any non-string or unrecognized state returns False rather
    than raising — the orchestrator should skip-with-reason, not crash."""
    return state in FIRING_STATES


# ---------------------------------------------------------------------------
# match_trigger() — raw event → candidates list
# ---------------------------------------------------------------------------

# Trigger types this orchestrator understands. The dispatch surface (GHA
# workflow, local hook bridge, /run command) is responsible for emitting
# one of these shapes.
_KNOWN_TRIGGER_TYPES = frozenset({"cron", "hook", "git-hook", "manual"})


def _path_filters_match(routine: dict, changed_files: list[str]) -> bool:
    """True iff any of `routine["path_filters"]` (fnmatch globs) matches
    any of `changed_files`. False if the routine declares no filters —
    a routine without `path_filters` is "fires on everything", not
    "fires on path matches", so it can't priority-elevate itself.

    fnmatch semantics: `*` matches within a path segment but does NOT
    cross `/`. Use `**` to span directories. Patterns are matched
    against the literal path string, not normalized — callers should
    pass repo-relative paths consistently.
    """
    filters = routine.get("path_filters") or []
    if not filters:
        return False
    for pattern in filters:
        for path in changed_files:
            if fnmatch.fnmatch(path, pattern):
                return True
    return False


def match_trigger(trigger: dict, routines: list[dict]) -> list[dict]:
    """Filter `routines` to the ones that should be considered for
    dispatch given this trigger. Returns a new list.

    Trigger shapes:
      {"type": "cron",     "cron_expr": "*/30 * * * *"}
      {"type": "hook",     "hook_event": "Stop"}
      {"type": "git-hook"}
      {"type": "manual",   "routine_ids": ["pr-watcher", "daily-digest"]}

    Cron matching is string-exact — equivalent expressions like
    `*/30 * * * *` and `0,30 * * * *` do NOT both match the same trigger,
    by design (the trigger system already chose one expression to fire
    on; matching by string keeps dispatch deterministic).

    Manual triggers ignore primitive entirely — if the user explicitly
    says 'run X', we trust them. State and automation_level still apply
    in tick().
    """
    if not isinstance(trigger, dict):
        raise ValueError(f"trigger must be a dict, got {type(trigger).__name__}")
    ttype = trigger.get("type")
    if ttype not in _KNOWN_TRIGGER_TYPES:
        raise ValueError(
            f"trigger.type must be one of {sorted(_KNOWN_TRIGGER_TYPES)}, "
            f"got {ttype!r}"
        )

    if ttype == "cron":
        cron_expr = trigger.get("cron_expr")
        if not cron_expr:
            raise ValueError("cron trigger requires cron_expr")
        return [
            r for r in routines
            if r.get("primitive") in ("scheduled", "pr-poll")
            and (r.get("trigger") or {}).get("cron") == cron_expr
        ]

    if ttype == "hook":
        event = trigger.get("hook_event")
        if not event:
            raise ValueError("hook trigger requires hook_event")
        return [
            r for r in routines
            if r.get("primitive") == "hook"
            and (r.get("trigger") or {}).get("event") == event
        ]

    if ttype == "git-hook":
        git_hooks = [r for r in routines if r.get("primitive") == "git-hook"]
        # PRD #10 priority rule 4: if the trigger reports the changed file
        # set AND at least one git-hook routine declares a `path_filters`
        # glob that matches one of those files, return ONLY the matching
        # routines (priority short-circuit). The canonical case is
        # `.iteration/goal.md` changed → meta-evolve fires alone, so
        # cron-style commit-tests/commit-lint don't steal the slot.
        #
        # If `changed_files` is missing OR empty (ambiguous: nothing
        # changed vs. trigger system couldn't compute), fall back to the
        # legacy behavior of returning every git-hook routine. Same when
        # no routine's filter matches — the catch-up routines still need
        # to run on plain code commits.
        changed_files = trigger.get("changed_files")
        if not changed_files:
            return git_hooks
        prioritized = [
            r for r in git_hooks if _path_filters_match(r, changed_files)
        ]
        return prioritized if prioritized else git_hooks

    # manual
    ids = trigger.get("routine_ids")
    if ids is None:
        raise ValueError("manual trigger requires routine_ids list")
    id_set = set(ids)
    # Preserve config order — the user's mental model is the order they
    # see in `status`, not the order they typed.
    return [r for r in routines if r.get("id") in id_set]


# ---------------------------------------------------------------------------
# tick() — the dispatch decision
# ---------------------------------------------------------------------------

# Routines that always run inside the user's session — surface is fixed.
_SESSION_PRIMITIVES = frozenset({"hook", "git-hook", "loop"})


def _local_iso_with_offset(now: dt.datetime) -> str:
    """Format a tz-aware datetime as `2026-05-10T17:03:00-0700`.
    Matches the rule pinned in routine-skill.md and state.py: never UTC `Z`."""
    if now.tzinfo is None:
        raise ValueError("tick requires a tz-aware `now`")
    # %z → ±HHMM; strftime for the rest. Don't use isoformat(): it emits
    # `+00:00` instead of `+0000` and our state validator rejects that.
    return now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _surface_for(routine: dict) -> str:
    """Resolve the surface a routine fires on. hook/git-hook/loop always
    run in-session (treated as 'local' for state-tracking purposes).
    Scheduled and pr-poll must declare execution_surface; we trust the
    sanity-checker has already enforced that."""
    if routine.get("primitive") in _SESSION_PRIMITIVES:
        return "local"
    return routine.get("execution_surface", "local")


def tick(
    now: dt.datetime,
    candidates: list[dict],
    state: dict,
    config: dict,
) -> dict:
    """Decide which candidate routines to dispatch on this tick.

    Inputs:
      now         — tz-aware datetime captured from the trigger.
      candidates  — routine dicts the trigger layer has filtered to fire.
      state       — current state.json (must already validate).
      config      — current config.yaml (must already validate, schema 4+).

    Output:
      {
        "decisions": [{routine_id, action, surface, reason}, ...],
        "new_state": <updated state dict>,
      }

    `decisions` preserves the order of `candidates`. Cost cap is consumed
    first-come-first-served — first routine to fit the remaining budget
    fires; latecomers are skipped with reason 'cost cap'.

    Pure: never mutates inputs. Calls no I/O. Idempotent for a given
    (now, candidates, state, config).
    """
    if now.tzinfo is None:
        raise ValueError("tick requires a tz-aware `now`")

    meta = config.get("meta", {})
    idle_window = meta.get("idle_window", "always")
    idle_window_tz = meta.get("idle_window_tz")
    gha_cap = meta.get("gha_minutes_cap", 60)
    config_kill = bool(meta.get("kill_switch", False))

    # Snapshot state so we never touch the caller's dict.
    new_state = {
        **state,
        "last_dispatch": dict(state.get("last_dispatch", {})),
    }
    new_state["last_event_id"] = state.get("last_event_id", 0) + 1

    # Roll the daily counter if we crossed midnight in idle_window_tz.
    # Skip when 'always' (no tz set) or no tz declared — no daily concept
    # to anchor against; counter just keeps accumulating until evolve
    # rolls it. (The schema layer enforces tz when window != 'always',
    # so this branch is the safe degenerate case.)
    reset_tz = idle_window_tz or "UTC"
    try:
        if should_reset_cost(now, state.get("gha_minutes_reset_date", ""), reset_tz):
            new_state["gha_minutes_used_today"] = 0
            new_state["gha_minutes_reset_date"] = (
                _to_zone(now, reset_tz).date().isoformat()
            )
    except ValueError:
        # Bad reset_date — leave counter alone; the validator will catch
        # this on the next state-write round-trip.
        pass

    state_kill = bool(state.get("kill_switch_active", False))
    kill_active = config_kill or state_kill

    decisions: list[dict] = []
    for routine in candidates:
        rid = routine.get("id", "?")
        # 1. Kill switch — short-circuit everything.
        if kill_active:
            decisions.append({
                "routine_id": rid,
                "action": "skip",
                "surface": None,
                "reason": "kill switch active",
            })
            continue
        # 2. FSM state — only ACTIVE/EVOLVING dispatch.
        rstate = routine.get("state", "ACTIVE")
        if not is_firing_state(rstate):
            decisions.append({
                "routine_id": rid,
                "action": "skip",
                "surface": None,
                "reason": f"state={rstate}",
            })
            continue
        # 3. Surface routing.
        surface = _surface_for(routine)
        # 4. GHA-only restrictions: idle window, cost cap.
        if surface == "gha":
            try:
                idle = is_in_idle_window(now, idle_window, reset_tz)
            except ValueError:
                # Malformed idle_window in config — treat as 'never idle'
                # (sanity check should have caught this; don't crash).
                idle = False
            if idle:
                decisions.append({
                    "routine_id": rid,
                    "action": "skip",
                    "surface": None,
                    "reason": f"in idle window ({idle_window} {reset_tz})",
                })
                continue
            est = routine.get("est_minutes", 5)
            current_used = new_state["gha_minutes_used_today"]
            if would_exceed_cap(current_used, est, gha_cap):
                decisions.append({
                    "routine_id": rid,
                    "action": "skip",
                    "surface": None,
                    "reason": (
                        f"cost cap reached: {current_used}+{est} > {gha_cap}"
                    ),
                })
                continue
            new_state["gha_minutes_used_today"] = current_used + est
        # 5. Fire — record dispatch.
        decisions.append({
            "routine_id": rid,
            "action": "fire",
            "surface": surface,
            "reason": "ok",
        })
        new_state["last_dispatch"][rid] = {
            "ts": _local_iso_with_offset(now),
            "surface": surface,
            "outcome": "ok",
        }

    return {"decisions": decisions, "new_state": new_state}


# ---------------------------------------------------------------------------
# CLI shim — what the GHA workflow (and ad-hoc local debug) calls.
#
# The pure functions above are what we test heavily. This block is the
# integration glue: argv → trigger dict, file I/O for state.json, JSON
# output the workflow can consume. Kept small on purpose; everything
# meaningful is delegated to the pure functions.
# ---------------------------------------------------------------------------

import argparse  # noqa: E402  (intentional: keep CLI deps off the import path
import json      # noqa: E402   for callers that just want pure functions)
import os        # noqa: E402
import pathlib   # noqa: E402
import sys       # noqa: E402
import tempfile  # noqa: E402

import yaml      # noqa: E402  (used by _cli_budget for atomic config rewrite)


def _parse_now(s: str) -> dt.datetime:
    """Parse `--now` strictly. Refuses naive datetimes and refuses UTC `Z`
    suffix (silent-UTC is exactly the footgun PRD #10 review called out).

    Accepts: `2026-05-10T14:00:00+0000`, `2026-05-10T14:00:00-0700`.
    Refuses: `2026-05-10T14:00:00`, `2026-05-10T14:00:00Z`.
    """
    if s.endswith("Z"):
        raise ValueError(
            f"--now must use ±HHMM offset (got {s!r}); UTC `Z` is banned "
            "to make local-clock vs UTC mismatches loud"
        )
    # fromisoformat in 3.11+ accepts ±HH:MM but not ±HHMM. Normalize.
    if len(s) >= 5 and s[-5] in ("+", "-") and s[-3] != ":":
        s_norm = s[:-2] + ":" + s[-2:]
    else:
        s_norm = s
    parsed = dt.datetime.fromisoformat(s_norm)
    if parsed.tzinfo is None:
        raise ValueError(
            f"--now must include a tz offset (got {s!r}); "
            "naive datetimes are refused to avoid silent-UTC bugs"
        )
    return parsed


def _load_yaml(path: str) -> dict:
    """Lazy import — pure-function callers shouldn't pay the yaml import cost."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_state_or_bootstrap(path: str, *, now: dt.datetime, tz_name: str) -> dict:
    """Load state.json from disk; bootstrap with initial_state() if missing.

    First-tick scenario: workflow runs before any state has been written.
    Lazy-imports state.py to keep the pure-function path import-free.
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # Bootstrap from state.initial_state(). Imported lazily so the
    # pure-function import path stays import-free.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_state_bootstrap", os.path.join(os.path.dirname(__file__), "state.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reset_date = _to_zone(now, tz_name).date().isoformat()
    return mod.initial_state(reset_date)


def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON to path atomically (tempfile + rename). Two writers
    (GHA + local) means a partial write would be corrupting; this is
    cheap insurance."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".json.tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _build_trigger(args: argparse.Namespace) -> dict:
    """Translate CLI flags → trigger dict consumed by match_trigger()."""
    t = args.trigger_type
    if t == "cron":
        return {"type": "cron", "cron_expr": args.cron_expr or ""}
    if t == "hook":
        return {"type": "hook", "hook_event": args.hook_event or ""}
    if t == "git-hook":
        # `--changed-files` is plumbed for PRD #10 priority rule 4. The
        # caller (GHA workflow / local post-commit) computes the diff
        # against the previous SHA and forwards a comma- or
        # newline-separated list. Empty string → no info, legacy match.
        raw = getattr(args, "changed_files", None) or ""
        files = [
            line.strip()
            for line in raw.replace(",", "\n").splitlines()
            if line.strip()
        ]
        trigger: dict = {"type": "git-hook"}
        if files:
            trigger["changed_files"] = files
        return trigger
    if t == "manual":
        ids = [s.strip() for s in (args.routine_ids or "").split(",") if s.strip()]
        return {"type": "manual", "routine_ids": ids}
    # Unknown — match_trigger will raise; we let that surface.
    return {"type": t}


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchestrator",
        description="auto-routines orchestrator — pure tick() with a thin CLI shim.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    tick_p = sub.add_parser(
        "tick",
        help="Run one orchestration tick: match trigger → decide → write state.",
        description=(
            "Reads config + state, matches the incoming trigger to "
            "candidate routines, runs tick(), writes new state, prints "
            "decisions JSON on stdout."
        ),
    )
    tick_p.add_argument("--config", required=True, help="Path to .iteration/config.yaml")
    tick_p.add_argument("--state", required=True, help="Path to .iteration/state.json (created if missing)")
    tick_p.add_argument(
        "--trigger-type", required=True,
        choices=["cron", "hook", "git-hook", "manual"],
        help="What woke us up.",
    )
    tick_p.add_argument("--cron-expr", help="Required when --trigger-type=cron")
    tick_p.add_argument("--hook-event", help="Required when --trigger-type=hook (e.g. Stop)")
    tick_p.add_argument(
        "--routine-ids",
        help="Comma-separated ids, required when --trigger-type=manual",
    )
    tick_p.add_argument(
        "--changed-files",
        help=(
            "Optional with --trigger-type=git-hook: comma- or "
            "newline-separated repo-relative paths changed in the "
            "triggering commit. When supplied, routines whose "
            "`path_filters` glob matches any of these paths are "
            "priority-selected (PRD #10 rule 4). When omitted/empty, "
            "every git-hook routine fires (legacy behavior)."
        ),
    )
    tick_p.add_argument(
        "--now",
        help=(
            "Override the clock for testing (must include ±HHMM offset; "
            "UTC `Z` refused). Defaults to the configured idle_window_tz now."
        ),
    )

    # budget: re-apply the cadence preset table to the live config.
    # Mirrors the per-tier cron mapping in SKILL.md "Budget → cadence
    # presets". Touches only routines named in the preset for that tier;
    # everything else stays byte-identical.
    budget_p = sub.add_parser(
        "budget",
        help="Re-apply the cadence preset for a budget tier to config.yaml.",
        description=(
            "Update `meta.budget` and rewrite cron expressions for "
            "routines named in the per-tier preset table from SKILL.md. "
            "Unrelated routines (git-hook routines, archetypes not in "
            "the preset) are left byte-identical."
        ),
    )
    budget_p.add_argument("--config", required=True, help="Path to .iteration/config.yaml")
    budget_p.add_argument(
        "--tier", required=True,
        help=(
            "Budget tier (low | medium | high | custom). low/medium/high "
            "apply preset crons; custom is a no-op on crons but still "
            "updates meta.budget. Validation happens in the handler so "
            "the error message routes to our injectable stderr."
        ),
    )

    # test-fire: manual one-shot dispatch plan for a single routine.
    # Read-only — does not touch state.json. Used by `/auto-routines
    # test-fire <id>` for debugging without waiting for cron.
    fire_p = sub.add_parser(
        "test-fire",
        help="Print the dispatch plan for one routine (debugging override).",
        description=(
            "Read the config, find the routine by id, and print the "
            "dispatch command shape (`claude --dangerously-skip-permissions "
            "-p \"/<routine_id>\"`) the user can copy-paste or pipe to a "
            "shell. Pure dry-run: NO state mutation, NO subprocess "
            "execution. Warns on STOPPED routines but still emits the plan "
            "(test-fire is a manual override)."
        ),
    )
    fire_p.add_argument("--config", required=True, help="Path to .iteration/config.yaml")
    fire_p.add_argument(
        "--routine-id", required=True,
        help="Routine id to fire (must exist in config.yaml routines list).",
    )

    # first-pr-eta: surface the first forward-driving routine's next-fire
    # schedule in the install welcome output. Pure read-only; sources the
    # `trigger.human` directly from config (sanity-check already pins it
    # present whenever cron is set). Maps config routine ids → archetype
    # `category` via the catalog so reactive routines are filtered out.
    eta_p = sub.add_parser(
        "first-pr-eta",
        help="Print a one-line ETA for the first auto-PR a fresh install will open.",
        description=(
            "Read config + catalog, find the first routine whose archetype "
            "has `category: forward-driving`, and print a one-line welcome "
            "message naming that routine's trigger.human. Used by SKILL.md "
            "step 8 to give the user a concrete expectation (\"your first "
            "auto-PR will land at ~6:00 PM\") instead of a generic finish "
            "line. Pure-script, no LLM tokens."
        ),
    )
    eta_p.add_argument("--config", required=True, help="Path to .iteration/config.yaml")
    eta_p.add_argument(
        "--catalog",
        default=str(pathlib.Path(__file__).resolve().parent.parent / "templates" / "routine-catalog.yaml"),
        help="Path to templates/routine-catalog.yaml (defaults to the in-tree catalog).",
    )

    # drain-evolve-requests: pure-script half of SKILL.md `Mode: evolve`
    # step 2. Parse, validate, emit a JSON plan; --apply truncates on
    # success. PRD goal.md (Coverage and correctness): "Add tests for
    # the `evolve` flow — drain evolve_requests.jsonl, perform the FSM
    # transitions, write a checkpoint, apply, verify."
    drain_p = sub.add_parser(
        "drain-evolve-requests",
        help="Drain .iteration/evolve_requests.jsonl and emit a JSON plan.",
        description=(
            "Read the evolve-requests jsonl, validate each line against "
            "the schema (ts, routine_id, reason, suggested), and emit "
            "one JSON object per valid request to stdout. Default is "
            "dry-run (file untouched); --apply truncates the file after "
            "a successful drain. Pure-script, no LLM tokens."
        ),
    )
    drain_p.add_argument(
        "--file",
        required=True,
        help="Path to .iteration/evolve_requests.jsonl. Missing file is a no-op.",
    )
    drain_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "After emitting the plan, truncate the file. Skipped when "
            "zero valid plan lines were produced (so a fix-and-retry is "
            "still possible)."
        ),
    )
    return p


# Cadence preset table — single source of truth for the budget command.
# Mirrors the table in SKILL.md "Budget → cadence presets". Each tier
# maps routine_id -> (cron, human). Routines not in a tier's preset are
# left untouched by the budget command (no silent downgrades of routines
# the preset doesn't mention).
BUDGET_PRESETS: dict[str, dict[str, tuple[str, str]]] = {
    "low": {
        "prd-implement": ("0 9 * * 1-5", "weekdays 9:00 AM"),
        # daily-digest and session-doc-drift are intentionally absent
        # at low tier — the install would skip them, but for a mid-run
        # `budget low` the user keeps any existing config they had.
    },
    "medium": {
        "prd-implement": ("0 */12 * * *", "every 12 hours"),
        "daily-digest": ("0 18 * * *", "6:00 PM daily"),
        "session-doc-drift": ("0 17 * * 1", "Mondays 5:00 PM"),
    },
    "high": {
        "prd-implement": ("0 */4 * * *", "every 4 hours"),
        "daily-digest": ("0 18 * * *", "6:00 PM daily"),
        "session-doc-drift": ("0 17 * * 1-5", "5:00 PM weekdays"),
    },
    "custom": {
        # custom tier is a no-op on crons; meta.budget is still updated
        # so the dashboard / interview see the new tier.
    },
}

# meta.cron preset (the meta-evolve daily/weekly cron, not a routine).
META_CRON_PRESETS: dict[str, tuple[str, str]] = {
    "low": ("0 9 * * 1", "Mondays 9:00 AM"),
    "medium": ("0 9 * * *", "9:00 AM daily"),
    "high": ("0 9 * * *", "9:00 AM daily"),
    # custom: no rewrite.
}


def _cli_budget(args, out, err) -> int:
    """Apply the cadence preset for `args.tier` to the config at
    `args.config`. Updates meta.budget; rewrites trigger.{cron,human}
    for routines in the preset; leaves everything else byte-identical.

    Argparse already validated the tier choice (`low|medium|high|custom`)
    so we don't re-check it here — but we still bounds-check the
    preset lookup so a future tier added to argparse without a preset
    entry fails loudly instead of silently no-op'ing."""
    if args.tier not in BUDGET_PRESETS:
        print(
            f"unknown budget tier: {args.tier!r} "
            f"(valid: {sorted(BUDGET_PRESETS)})",
            file=err,
        )
        return 1

    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    if config is None:
        print(f"config is empty: {args.config}", file=err)
        return 1

    preset = BUDGET_PRESETS[args.tier]

    # 1. Update meta.budget. Always — even for `custom`.
    config.setdefault("meta", {})["budget"] = args.tier

    # 2. Update meta.cron / meta.human if a preset exists for this tier.
    meta_preset = META_CRON_PRESETS.get(args.tier)
    if meta_preset:
        cron, human = meta_preset
        config["meta"]["cron"] = cron
        config["meta"]["human"] = human

    # 3. Walk routines; rewrite the ones the preset names. Capture the
    # task_id + new cron for each so we can emit the MCP update plan
    # for the SKILL.md follow-up step.
    routines = config.get("routines", []) or []
    touched: list[str] = []
    plan_entries: list[dict[str, str]] = []
    warnings: list[str] = []
    for routine in routines:
        rid = routine.get("id")
        if rid not in preset:
            continue
        cron, human = preset[rid]
        trigger = routine.setdefault("trigger", {})
        trigger["cron"] = cron
        trigger["human"] = human
        touched.append(rid)

        # Plan emission. Routines lacking a stored `task_id` (hand-
        # edited config, or pre-orchestrator install) get a warning
        # rather than a silent skip — silently leaving the live MCP
        # cron stale is the bug class the warning prevents.
        task_id = routine.get("task_id")
        if task_id:
            plan_entries.append(
                {
                    "routine_id": rid,
                    "task_id": task_id,
                    "cron": cron,
                    "human": human,
                }
            )
        else:
            warnings.append(
                f"# warn: routine {rid!r} has no stored task_id — "
                f"cannot emit update_scheduled_task plan line. "
                f"Re-install or set task_id manually."
            )

    # 4. Write back to disk. Use atomic-via-tempfile-and-rename so a
    # crash mid-write doesn't leave a half-rewritten config.
    try:
        _atomic_write_yaml(args.config, config)
    except OSError as e:
        print(f"config write failed: {e}", file=err)
        return 1

    out.write(
        f"# budget set to {args.tier!r}\n"
        f"# touched routines: {touched or '(none — preset is empty)'}\n"
        f"# meta.cron updated: {bool(meta_preset)}\n"
    )

    # 5. Emit the MCP update plan. One JSON object per line — stable,
    # parseable, no ad-hoc string format for the LLM to misread. The
    # marker line `mcp-plan:` separates the human summary above from
    # the machine-parseable block below; SKILL.md `Mode: budget` keys
    # off this marker.
    out.write("mcp-plan:\n")
    for w in warnings:
        out.write(w + "\n")
    for entry in plan_entries:
        out.write(json.dumps(entry, sort_keys=True) + "\n")
    return 0


def _atomic_write_yaml(path: str, data: dict) -> None:
    """Atomic write of `data` as YAML to `path`. Mirrors
    `_atomic_write_json` — tempfile in the same directory then
    `os.replace` so an interrupted write doesn't leave a half-file."""
    p = pathlib.Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = yaml.safe_dump(data, sort_keys=False)
    tmp.write_text(text)
    os.replace(tmp, p)


def _cli_first_pr_eta(args, out, err) -> int:
    """Print a one-line ETA for the first auto-PR a fresh install will open.

    Reads config + catalog. Finds the first routine whose archetype has
    `category: forward-driving` (in config order — deterministic, no
    cron-arithmetic), and prints a one-line welcome message naming that
    routine's trigger.human. If no forward-driving routine is installed,
    prints a stub ("reactive-only install") and returns 0 — a
    reactive-only install is a valid configuration, just not one that
    auto-opens PRs on a schedule.

    Pure read-only. PRD goal.md (Skill UX): "Surface the first routine
    PR opened by a fresh install in the welcome output".
    """
    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1
    try:
        catalog = _load_yaml(args.catalog)
    except (OSError, Exception) as e:
        print(f"catalog load failed: {e}", file=err)
        return 1

    # Map archetype id → category. Catalog may evolve; default unknown
    # ids to None so we silently skip them rather than crash on a user
    # config that references a custom or out-of-tree routine.
    categories: dict[str, str] = {}
    for arch in (catalog or {}).get("archetypes", []) or []:
        rid = arch.get("id")
        cat = arch.get("category")
        if rid and cat:
            categories[rid] = cat

    routines = (config or {}).get("routines", []) or []
    forward = next(
        (r for r in routines if categories.get(r.get("id")) == "forward-driving"),
        None,
    )

    if forward is None:
        # Valid config, just no forward-driving routine. Tell the
        # operator explicitly so the empty ETA doesn't read as a bug.
        print(
            "No forward-driving routine installed — reactive-only install. "
            "Auto-PRs won't open on a schedule; routines fire on commits / "
            "PR events.",
            file=out,
        )
        return 0

    human = (forward.get("trigger") or {}).get("human") or "(no schedule set)"
    rid = forward.get("id", "?")
    print(
        f"Your first auto-PR (from `{rid}`) will land at: {human}.",
        file=out,
    )
    return 0


# Required keys per evolve-request line. Schema mirrors SKILL.md
# "Mid-run self-evolution" — a request must name when, who, why, and
# what to do. Missing any of these makes the request un-actionable;
# the LLM step can't apply an anonymous suggestion to no routine.
_EVOLVE_REQUEST_REQUIRED = ("ts", "routine_id", "reason", "suggested")


def _cli_drain_evolve_requests(args, out, err) -> int:
    """Drain `.iteration/evolve_requests.jsonl` and emit a JSON plan.

    Missing file → 0 plan lines, exit 0 (a fresh repo has no requests).
    Empty file → 0 plan lines, exit 0.
    Malformed lines → `# warn:` line on stdout, valid lines still emit.
    --apply → truncate the file ONLY if at least one valid plan line
              was produced (silently swallowing every malformed request
              is the worst possible failure mode).

    Pure read+write; no MCP, no network."""
    path = pathlib.Path(args.file)
    if not path.exists():
        # No file = no requests. Quiet success — SKILL.md step 2 can
        # call this unconditionally without a `[ -f ... ]` guard.
        return 0

    plan_entries: list[dict] = []
    warnings: list[str] = []

    try:
        raw = path.read_text()
    except OSError as e:
        print(f"drain failed: {e}", file=err)
        return 1

    for lineno, line in enumerate(raw.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            entry = json.loads(s)
        except json.JSONDecodeError as e:
            warnings.append(
                f"# warn: line {lineno}: invalid JSON ({e.msg}) — skipped"
            )
            continue
        if not isinstance(entry, dict):
            warnings.append(
                f"# warn: line {lineno}: not a JSON object — skipped"
            )
            continue
        missing = [k for k in _EVOLVE_REQUEST_REQUIRED if k not in entry]
        if missing:
            warnings.append(
                f"# warn: line {lineno}: missing required field(s) "
                f"{missing} — skipped"
            )
            continue
        # Emit a normalized entry (sorted keys) so the LLM step has a
        # stable shape per line.
        plan_entries.append(
            {k: entry[k] for k in _EVOLVE_REQUEST_REQUIRED}
        )

    # Emit warnings first, then plan lines — keeps malformed-input
    # surface visible at the top of the output.
    for w in warnings:
        out.write(w + "\n")
    for entry in plan_entries:
        out.write(json.dumps(entry, sort_keys=True) + "\n")

    # Truncate only if --apply was passed AND at least one valid plan
    # line was produced. The "no valid lines" guard prevents silently
    # discarding a file the user filled with malformed requests they'd
    # want to fix and retry.
    if args.apply and plan_entries:
        try:
            # Atomic-via-tempfile-and-rename so a crash mid-write
            # doesn't lose un-processed entries.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("")
            os.replace(tmp, path)
        except OSError as e:
            print(f"truncate failed: {e}", file=err)
            return 1

    return 0


def _cli_test_fire(args, out, err) -> int:
    """Manual one-shot dispatch plan for `/auto-routines test-fire <id>`.

    Read-only: no state.json touch, no subprocess execution. Just prints
    the dispatch command the user can copy-paste so they can fire one
    routine without waiting for cron. STOPPED routines warn but still
    emit a plan (this is a manual override — silent refusal would be
    worse than a noisy warning)."""
    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    routines = (config or {}).get("routines", []) or []
    match = next((r for r in routines if r.get("id") == args.routine_id), None)
    if match is None:
        known = ", ".join(r.get("id", "?") for r in routines) or "(none)"
        print(
            f"unknown routine id: {args.routine_id!r} "
            f"(known routines: {known})",
            file=err,
        )
        return 1

    state = match.get("state", "?")
    if state not in FIRING_STATES:
        # Manual override: warn but proceed.
        print(
            f"warning: routine {args.routine_id!r} is in state "
            f"{state!r} (not in FIRING_STATES={sorted(FIRING_STATES)}). "
            "test-fire is a manual override, so the plan below would still "
            "run if executed — but it would not fire on a real trigger.",
            file=err,
        )

    primitive = match.get("primitive", "?")
    surface = match.get("execution_surface") or "local"
    automation = match.get("automation_level", "?")
    purpose = match.get("purpose", "")

    # Mirror the post-commit-hook dispatch shape so test-fire output and
    # a real local fire stay congruent. The slash-command form (`/<id>`)
    # is what the SKILL.md per-routine file responds to.
    cmd = f'claude --dangerously-skip-permissions -p "/{args.routine_id}"'

    lines = [
        f"# test-fire dispatch plan for routine: {args.routine_id}",
        f"#   primitive:   {primitive}",
        f"#   state:       {state}",
        f"#   surface:     {surface}",
        f"#   automation:  {automation}",
        f"#   purpose:     {purpose}",
        "#",
        "# Run this command to fire the routine now (or pipe to bash):",
        cmd,
    ]
    out.write("\n".join(lines) + "\n")
    return 0


def cli_main(
    argv: list[str],
    *,
    stdout=None,
    stderr=None,
) -> int:
    """Entry point for the CLI. Returns an exit code; does not call sys.exit
    (so tests can drive it without process boundaries).

    `stdout`/`stderr` are injectable so tests can capture output without
    monkeypatching sys.* globals."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = _make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse calls sys.exit on error; absorb so callers get a code
        # rather than a crash. argparse already wrote to stderr.
        return int(e.code) if e.code is not None else 2

    if args.command == "test-fire":
        return _cli_test_fire(args, out, err)

    if args.command == "budget":
        return _cli_budget(args, out, err)

    if args.command == "first-pr-eta":
        return _cli_first_pr_eta(args, out, err)

    if args.command == "drain-evolve-requests":
        return _cli_drain_evolve_requests(args, out, err)

    if args.command != "tick":
        print(f"unknown command: {args.command}", file=err)
        return 2

    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    meta = (config or {}).get("meta", {}) or {}
    tz_name = meta.get("idle_window_tz") or "UTC"

    # Resolve `now` — explicit override or current time in idle_window_tz.
    try:
        if args.now:
            now = _parse_now(args.now)
        else:
            now = dt.datetime.now(tz=ZoneInfo(tz_name))
    except (ValueError, ZoneInfoNotFoundError) as e:
        print(f"--now invalid: {e}", file=err)
        return 1

    try:
        state = _load_state_or_bootstrap(args.state, now=now, tz_name=tz_name)
    except OSError as e:
        print(f"state load failed: {e}", file=err)
        return 1

    trigger = _build_trigger(args)
    routines = (config or {}).get("routines", []) or []

    try:
        candidates = match_trigger(trigger, routines)
    except ValueError as e:
        print(f"trigger error: {e}", file=err)
        return 1

    try:
        result = tick(now, candidates, state, config)
    except ValueError as e:
        print(f"tick error: {e}", file=err)
        return 1

    try:
        _atomic_write_json(args.state, result["new_state"])
    except OSError as e:
        print(f"state write failed: {e}", file=err)
        return 1

    json.dump({"decisions": result["decisions"]}, out)
    out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
