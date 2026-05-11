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
import pathlib
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Mirrors sanity.FIRING_STATES — duplicated here so the orchestrator
# has zero imports from the rest of the repo (the test pins both).
FIRING_STATES: frozenset = frozenset({"ACTIVE", "EVOLVING"})

# Sealed union of `success_criterion.kind` values. Mirrored in
# `scripts/sanity-check.py::PREDICATE_KINDS` and documented in
# `templates/routine-preamble.md::## Success criteria`. The drift
# detector in `tests/test_preamble_predicates_matches_sanity.py`
# pins all three surfaces together.
#
# - `all-tasks-checked` — orchestrator-enforced (issue #75)
# - `coverage-above` / `pr-merged-count` / `no-failures-n-days` —
#   declared here, evaluator branches land in issue #76
# - `llm-narrative` — fallback for unstructured prose; the
#   evaluator returns None so the meta-agent (LLM) handles it
PREDICATE_KINDS: frozenset = frozenset({
    "all-tasks-checked",
    "coverage-above",
    "pr-merged-count",
    "no-failures-n-days",
    "llm-narrative",
})

_IDLE_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
# Matches a GitHub-flavoured-markdown task list checkbox at the start
# of a (possibly indented) line: `- [ ]`, `- [x]`, `- [X]`, also `* [x]`,
# `+ [x]`, and ordered-list variants like `1. [x]`.
_TASK_CHECKBOX_RE = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+\[(?P<mark>[ xX])\]\s",
    re.M,
)


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
# success_criterion — structured predicate union (issue #75)
# ---------------------------------------------------------------------------


def normalize_success_criterion(value: Any) -> Optional[dict]:
    """Coerce a `success_criterion` field into the canonical
    `{kind, args}` shape, or None if the field is absent.

    Three input shapes are accepted:

    - `None`               → `None` (no criterion; routine runs forever)
    - `"<prose>"` (str)    → `{kind: 'llm-narrative', args: {prose: <prose>}}`
      This is the backward-compat path — every existing routine config
      carries free-text prose in this field.
    - `{kind, args}` dict  → passed through after kind validation; if
      `args` is missing it defaults to `{}` (predicate evaluator
      supplies per-kind defaults).

    An unknown `kind` raises `ValueError` so the bug surfaces at
    config-load time instead of at evolve time."""
    if value is None:
        return None
    if isinstance(value, str):
        # Round-trip even empty strings — the evaluator turns them back
        # into "no predicate" via the empty-prose check.
        return {"kind": "llm-narrative", "args": {"prose": value}}
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind not in PREDICATE_KINDS:
            raise ValueError(
                f"success_criterion.kind must be one of "
                f"{sorted(PREDICATE_KINDS)}, got {kind!r}"
            )
        args = value.get("args")
        if args is None:
            args = {}
        elif not isinstance(args, dict):
            raise ValueError(
                f"success_criterion.args must be a mapping, got {type(args).__name__}"
            )
        return {"kind": kind, "args": args}
    raise ValueError(
        f"success_criterion must be a string, mapping, or null; got "
        f"{type(value).__name__}"
    )


def _count_checkbox_completion(text: str) -> tuple[int, int]:
    """Return `(checked, total)` GFM task-list checkboxes in `text`.

    Both `[x]` and `[X]` count as checked. Indented sub-tasks count too —
    a PRD with `- [x] top\\n  - [x] sub\\n` has total=2, checked=2."""
    total = 0
    checked = 0
    for m in _TASK_CHECKBOX_RE.finditer(text):
        total += 1
        if m.group("mark") in ("x", "X"):
            checked += 1
    return checked, total


def _eval_coverage_above(args: dict, context: dict) -> bool:
    """`coverage-above` — True iff `args.file` parses to a coverage
    percentage ≥ `args.threshold`. Inclusive at the boundary (exactly
    at threshold counts as passing — otherwise threshold=80 + actual=80%
    never completes, which is a footgun).

    Two source formats auto-detected by first non-whitespace byte:

    - Cobertura XML (`<coverage line-rate="0.84" ...>`) — what
      `pytest-cov --cov-report=xml` emits.
    - `coverage report` stdout — bottom-line `TOTAL ... 84%`.

    Default threshold is 80 (the conventional round number). Default
    file is `coverage.xml`. Missing / unparseable file returns False
    (predicate eval is observational, not assertive)."""
    file_path = args.get("file") or "coverage.xml"
    threshold = args.get("threshold")
    if threshold is None:
        threshold = 80
    path = pathlib.Path(file_path)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    pct = _parse_coverage_percent(text)
    if pct is None:
        return False
    return pct >= float(threshold)


def _parse_coverage_percent(text: str) -> Optional[float]:
    """Return the overall coverage as a percent (0-100), or None if
    the file isn't recognized as either Cobertura XML or
    `coverage report` stdout. Pure: no I/O."""
    stripped = text.lstrip()
    if stripped.startswith("<"):
        # Cobertura: <coverage line-rate="0.84" ...>
        m = re.search(r'<coverage[^>]*\bline-rate\s*=\s*"([0-9.]+)"', text)
        if not m:
            return None
        try:
            rate = float(m.group(1))
        except ValueError:
            return None
        # Cobertura's line-rate is a 0..1 ratio; promote to percent.
        return rate * 100.0
    # Plain-text `coverage report`: a bottom-line `TOTAL ... NN%` line.
    # Match the last TOTAL row so a stray "TOTAL" elsewhere doesn't win.
    last_total_pct: Optional[float] = None
    for line in text.splitlines():
        m = re.match(r"^\s*TOTAL\b.*?(\d+(?:\.\d+)?)\s*%\s*$", line)
        if m:
            try:
                last_total_pct = float(m.group(1))
            except ValueError:
                pass
    return last_total_pct


def _load_log_entries(log_path: str) -> list[dict]:
    """Read `.iteration/log.jsonl` and return parsed entries. Lines
    that don't parse as JSON are skipped silently — a half-written
    log line must not crash the predicate evaluator."""
    import json
    path = pathlib.Path(log_path)
    if not path.is_file():
        return []
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []
    return entries


def _eval_pr_merged_count(
    args: dict, context: dict, routine_id: str
) -> bool:
    """`pr-merged-count` — True iff the count of log entries belonging
    to this routine, with `outcome: ok` AND a `pr_url` field, meets or
    exceeds `args.count`. Scoped by routine id — without scoping, every
    routine in a multi-archetype install would "complete" the moment
    the global PR count crossed the threshold."""
    log_path = context.get("log_path") or ".iteration/log.jsonl"
    target = int(args.get("count", 1))
    count = 0
    for entry in _load_log_entries(log_path):
        if entry.get("routine") != routine_id:
            continue
        if entry.get("outcome") != "ok":
            continue
        if not entry.get("pr_url"):
            continue
        count += 1
        if count >= target:
            return True
    return False


def _parse_iso_local(ts: str) -> Optional[dt.datetime]:
    """Parse an ISO 8601 string with offset (the canonical log.jsonl
    `ts` format — see `templates/routine-preamble.md`). Returns a
    tz-aware datetime or None on parse failure. Accepts both
    `+HHMM` (date(1) default) and `+HH:MM` (strict ISO)."""
    if not isinstance(ts, str) or not ts:
        return None
    candidate = ts
    # `date +%Y-%m-%dT%H:%M:%S%z` emits `+HHMM` with no colon.
    # `datetime.fromisoformat` on 3.11+ accepts both, but we
    # normalize for 3.9/3.10 compatibility.
    m = re.match(r"^(.+)([+-])(\d{2})(\d{2})$", candidate)
    if m and ":" not in m.group(0)[-5:]:
        candidate = f"{m.group(1)}{m.group(2)}{m.group(3)}:{m.group(4)}"
    try:
        out = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if out.tzinfo is None:
        return None
    return out


def _eval_no_failures_n_days(
    args: dict, context: dict, routine_id: str
) -> bool:
    """`no-failures-n-days` — True iff:

    - at least one entry for this routine falls inside the window
      `(now - days, now]`, AND
    - no entry in that window has `outcome: err`.

    "At least one entry in window" prevents auto-completing a fresh
    install: with no history at all, we don't know the routine is
    stable, only that we haven't seen it fail (or fire). Same
    reasoning as `all-tasks-checked` rejecting empty goal files."""
    log_path = context.get("log_path") or ".iteration/log.jsonl"
    days = int(args.get("days", 7))
    now_str = context.get("now")
    if now_str:
        now = _parse_iso_local(now_str)
        if now is None:
            return False
    else:
        now = dt.datetime.now().astimezone()
    cutoff = now - dt.timedelta(days=days)
    saw_in_window = False
    for entry in _load_log_entries(log_path):
        if entry.get("routine") != routine_id:
            continue
        ts = _parse_iso_local(entry.get("ts", ""))
        if ts is None:
            continue
        if ts <= cutoff:
            continue
        saw_in_window = True
        if entry.get("outcome") == "err":
            return False
    return saw_in_window


def _eval_all_tasks_checked(args: dict, context: dict) -> bool:
    """`all-tasks-checked` — True iff the referenced markdown file
    exists AND has ≥1 checkbox AND every checkbox is checked.

    `args.file` defaults to `.iteration/goal.md` (relative to cwd).
    A missing or unreadable file returns False rather than raising —
    predicate eval is a best-effort observation, not a contract."""
    file_path = args.get("file") or ".iteration/goal.md"
    path = pathlib.Path(file_path)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    checked, total = _count_checkbox_completion(text)
    if total == 0:
        return False
    return checked == total


def evaluate_success_criterion(
    routine_config: dict,
    context: Optional[dict] = None,
) -> Optional[bool]:
    """Evaluate a routine's `success_criterion` against the current
    repo state. Returns:

    - `True`  — predicate satisfied; routine should transition
      ACTIVE → COMPLETED (caller's responsibility to apply).
    - `False` — predicate not yet satisfied; routine keeps firing.
    - `None`  — no orchestrator-side decision possible: the criterion
      is either absent, empty, or `llm-narrative` (deferred to the
      meta-agent's LLM evolve step).

    The function is pure with respect to `routine_config` and
    `context`; the only side effect is a single read of the goal file
    when `kind: all-tasks-checked` is evaluated. Unknown kinds raise
    `ValueError` — sealed union, exhaustive match.

    Issue #75 implements `all-tasks-checked` and `llm-narrative`.
    Issue #76 adds `coverage-above`, `pr-merged-count`,
    `no-failures-n-days`."""
    if context is None:
        context = {}
    raw = routine_config.get("success_criterion")
    sc = normalize_success_criterion(raw)
    if sc is None:
        return None
    kind = sc["kind"]
    args = sc["args"]
    if kind == "llm-narrative":
        # Auto-wrapped empty prose is semantically "no criterion".
        if not (args.get("prose") or "").strip():
            return None
        return None
    if kind == "all-tasks-checked":
        return _eval_all_tasks_checked(args, context)
    if kind == "coverage-above":
        return _eval_coverage_above(args, context)
    if kind == "pr-merged-count":
        return _eval_pr_merged_count(args, context, routine_config.get("id", ""))
    if kind == "no-failures-n-days":
        return _eval_no_failures_n_days(
            args, context, routine_config.get("id", "")
        )
    raise ValueError(
        f"unhandled predicate kind {kind!r} — orchestrator.PREDICATE_KINDS "
        f"is {sorted(PREDICATE_KINDS)}; evaluator must match"
    )


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
# Harness detection — stack → canonical preset (issue #78).
#
# A pure function over a repo path + the catalog's `harness_presets` list.
# Precedence: first match wins, so the catalog author controls priority
# (python-pytest declared first wins on a polyglot repo). A "match" is:
# at least one of the preset's `stack_hints` paths exists under the repo
# root. Hints ending in `/` are treated as directory tests; everything
# else as a regular path test (existing file OR directory). Keeping the
# rule simple — sophisticated heuristics belong in the interview, not in
# the express path.
# ---------------------------------------------------------------------------


def detect_harness(repo_path: str, presets: list) -> Any:
    """Return the first preset whose `stack_hints` are satisfied by the
    filesystem under `repo_path`, or None if no preset matches.

    Pure: no globals, no cwd dependence. Tests pin precedence — first
    match in catalog order wins."""
    import os as _os  # local import — keep module-import path clean

    if not presets:
        return None
    root = _os.fspath(repo_path)
    for preset in presets:
        hints = preset.get("stack_hints") or []
        for hint in hints:
            # A trailing slash means "this must be a directory". Otherwise
            # any path entry (file or dir) counts as a match.
            candidate = _os.path.join(root, hint.rstrip("/"))
            if hint.endswith("/"):
                if _os.path.isdir(candidate):
                    return preset
            else:
                if _os.path.exists(candidate):
                    return preset
    return None


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
import stat      # noqa: E402  (used by install-doctor for hook exec-bit check)
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

    # recompute-cadence: value-based throttle/amplify within the
    # current budget tier. Reads each routine's recent log entries,
    # computes useful_fires/total_fires, picks the corresponding rung
    # on `CADENCE_LADDERS[tier]`, writes back. Bounded by tier — never
    # crosses `budget` command's cap.
    recompute_p = sub.add_parser(
        "recompute-cadence",
        help="Retune cron per routine based on recent value signal (within budget tier).",
        description=(
            "Reads `.iteration/log.jsonl` and recomputes each routine's "
            "cron position on its budget tier's cadence ladder. "
            "value_rate = useful_fires / total_fires where useful means "
            "`outcome: ok` AND `increment_signal: true`. Idempotent: if "
            "the recomputed cron equals the current cron, no write."
        ),
    )
    recompute_p.add_argument(
        "--config", required=True, help="Path to .iteration/config.yaml"
    )
    recompute_p.add_argument(
        "--log", default=".iteration/log.jsonl",
        help="Path to log.jsonl (default: .iteration/log.jsonl)",
    )
    recompute_p.add_argument(
        "--window", default=20, type=int,
        help="How many recent entries per routine to consider (default: 20)",
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

    # fsm-plan: pure-script half of SKILL.md `Mode: evolve` step 4.
    # For each ACTIVE routine, check `runs since last_useful_iter >=
    # stagnation_threshold` and emit a JSON transition plan. The OTHER
    # FSM transitions (reactivation, success_criterion-met) stay LLM-
    # driven because they require natural-language signal interpretation;
    # stagnation is pure arithmetic.
    #
    # PRD goal.md (Coverage and correctness): "Add tests for the
    # `evolve` flow — drain evolve_requests.jsonl, perform the FSM
    # transitions, write a checkpoint, apply, verify." This subcommand
    # is the FSM-transitions half (deterministic subset).
    fsm_p = sub.add_parser(
        "fsm-plan",
        help="Emit ACTIVE→STAGNANT transition plan for stagnant routines.",
        description=(
            "Scan routines in `--config`; for each ACTIVE routine with "
            "runs since last_useful_iter >= stagnation_threshold (per "
            "routine, falling back to meta.default_stagnation_threshold), "
            "emit one JSON object on stdout. Pure-script, read-only, no "
            "LLM tokens — the SKILL.md Mode: evolve step iterates these "
            "plan lines instead of doing stats arithmetic itself."
        ),
    )
    fsm_p.add_argument(
        "--config",
        required=True,
        help="Path to .iteration/config.yaml",
    )

    # apply-fsm-plan: write-half of the evolve flow. Consumes JSONL
    # plan lines (the output of `fsm-plan` or a hand-crafted file) and
    # mutates `routines[i].state` in config.yaml atomically.
    #
    # Pre-flight validates the WHOLE plan before touching the file —
    # one invalid transition aborts everything, so config.yaml is
    # never half-applied. The alternative (mutate as we go) would
    # leave the user with a config that's neither the old nor the new
    # FSM state, and recovery would mean hand-editing YAML.
    #
    # PRD `.iteration/goal.md` (Coverage and correctness): "Add tests
    # for the `evolve` flow — drain evolve_requests.jsonl, perform
    # the FSM transitions, write a checkpoint, apply, verify." This
    # is the **apply** half; verify (read-back) is a separate slice.
    apply_p = sub.add_parser(
        "apply-fsm-plan",
        help="Apply FSM transitions from a JSONL plan to config.yaml.",
        description=(
            "Read JSONL transition lines from `--plan` (or stdin via "
            "`-`), validate every line against the current config, and "
            "atomically rewrite config.yaml with the new states. "
            "All-or-nothing: a single invalid transition aborts the "
            "whole plan. Emits one JSON result record per plan line "
            "on stdout; exit 0 iff every transition applied."
        ),
    )
    apply_p.add_argument(
        "--config",
        required=True,
        help="Path to .iteration/config.yaml (rewritten atomically).",
    )
    apply_p.add_argument(
        "--plan",
        required=True,
        help=(
            "Path to JSONL plan file, or `-` to read from stdin. Each "
            "line: {routine_id, from, to[, reason]}."
        ),
    )

    # verify-fsm-state: read-side companion to `apply-fsm-plan`.
    # Consumes the SAME JSONL plan; treats `to` as the EXPECTED
    # current state and asserts the config matches. Closes the
    # evolve flow's "verify" half (PRD goal.md).
    verify_p = sub.add_parser(
        "verify-fsm-state",
        help="Verify config.yaml states match the expected `to` in a plan.",
        description=(
            "Read JSONL transition lines from `--plan` (or stdin via "
            "`-`); for each line, assert that the routine's current "
            "state in `--config` equals the line's `to`. Mirrors "
            "`apply-fsm-plan`'s JSONL interface so the apply and the "
            "verify share one plan file. Emits one JSON record per "
            "assertion; exit 0 iff every assertion holds."
        ),
    )
    verify_p.add_argument(
        "--config",
        required=True,
        help="Path to .iteration/config.yaml (read-only).",
    )
    verify_p.add_argument(
        "--plan",
        required=True,
        help=(
            "Path to JSONL plan file, or `-` to read from stdin. Each "
            "line: {routine_id, to, ...}. `from` is ignored by verify."
        ),
    )

    # open-pr: deterministic wrapper around `gh pr create`. Routines and
    # the install procedure can call this instead of asking the LLM to
    # assemble the invocation. Tests mock subprocess.run to pin the
    # call shape without needing a real GitHub PR (PRD goal.md
    # "Coverage and correctness").
    pr_p = sub.add_parser(
        "open-pr",
        help="Open a GitHub PR via `gh pr create` (subprocess-mockable wrapper).",
        description=(
            "Assemble a `gh pr create` invocation with the canonical "
            "flag shape (--head, --base, --title, --body), auto-"
            "resolving --base from origin's default branch when "
            "omitted. Emits the PR URL on stdout on success. Never "
            "passes --repo (in-repo only). All subprocess calls go "
            "through `subprocess.run` so tests can mock the call "
            "shape via monkeypatch."
        ),
    )
    pr_p.add_argument(
        "--head", required=True,
        help="The branch the PR introduces (e.g. routines/foo).",
    )
    pr_p.add_argument(
        "--title", required=True,
        help="PR title (conventional-commit summary).",
    )
    pr_p.add_argument(
        "--body", required=True,
        help="PR body (markdown — explain why, then a checklist).",
    )
    pr_p.add_argument(
        "--base", default=None,
        help=(
            "Target branch. If omitted, resolved from "
            "`git symbolic-ref --short refs/remotes/origin/HEAD` "
            "(supports repos with main / master / trunk / etc.)."
        ),
    )

    cp_p = sub.add_parser(
        "checkpoint-append",
        help="Append a row to .iteration/checkpoints.md (pure-data, atomic).",
        description=(
            "Append a checkpoint row to a Markdown-table checkpoints "
            "file. Handles the two pieces the LLM keeps fat-fingering: "
            "the iter number (max(existing)+1, not count) and the "
            "timestamp (local ISO-8601 with offset, never UTC `Z`). "
            "Initializes the table header if the file is missing. "
            "Echoes the appended row on stdout."
        ),
    )
    cp_p.add_argument(
        "--file", required=True,
        help="Path to checkpoints.md (will be created if missing).",
    )
    cp_p.add_argument(
        "--sha", required=True,
        help="Commit SHA this checkpoint refers to (revert target).",
    )
    cp_p.add_argument(
        "--summary", required=True,
        help=(
            "One-line human-readable summary. Must not contain a "
            "literal `|` (would break the Markdown table)."
        ),
    )

    rrs_p = sub.add_parser(
        "render-routine-skill",
        help="Render templates/routine-skill.md into .claude/skills/<id>/SKILL.md.",
        description=(
            "Pure-script placeholder substitution for per-routine "
            "SKILL.md rendering (install step 6f). Pulls the routine "
            "config from `--config`, the prompt_body from `--catalog`, "
            "and the template from `--template`; writes the rendered "
            "SKILL.md to `--out` atomically. Refuses if any `{{...}}` "
            "placeholder remains unsubstituted — the PRD's `no "
            "placeholders` install acceptance criterion made concrete."
        ),
    )
    rrs_p.add_argument(
        "--config", required=True,
        help="Path to .iteration/config.yaml.",
    )
    rrs_p.add_argument(
        "--catalog", required=True,
        help="Path to templates/routine-catalog.yaml (source of prompt_body).",
    )
    rrs_p.add_argument(
        "--template", required=True,
        help="Path to templates/routine-skill.md.",
    )
    rrs_p.add_argument(
        "--routine", required=True,
        help="Routine id (must appear in config.yaml).",
    )
    rrs_p.add_argument(
        "--out", required=True,
        help="Destination path (typically .claude/skills/<id>/SKILL.md).",
    )
    rrs_p.add_argument(
        "--installed-at", default=None,
        help=(
            "Optional explicit ISO-8601 timestamp for the description "
            "line. If omitted, uses local-now with offset (never UTC "
            "`Z` — SKILL.md is explicit about local-machine readability)."
        ),
    )

    id_p = sub.add_parser(
        "install-doctor",
        help="Audit a repo for a healthy auto-routines install.",
        description=(
            "Walk the filesystem of a repo and verify every artifact "
            "that auto-routines's install procedure is supposed to "
            "land. Emits one JSON line per check on stdout. Exit 0 "
            "when every check passes, exit 1 otherwise. The "
            "deterministic core of the PRD's 'init integration test' "
            "acceptance criteria — usable on its own as "
            "`/auto-routines doctor`, and re-used by the future full "
            "integration test for its assertion half."
        ),
    )
    id_p.add_argument(
        "--repo-root", required=True,
        help=(
            "Path to the repo root to audit. Required — never default "
            "to cwd (auditing the wrong repo by accident is worse "
            "than a noisy argparse error)."
        ),
    )

    # detect-harness (issue #78) — stack-aware express install path.
    # Without --apply, prints the detected preset + archetype set so the
    # user (or a wrapper script) can decide. With --apply, writes a
    # minimal `.iteration/config.yaml` non-interactively. The catalog's
    # `harness_presets:` table is the single source of truth — see
    # `tests/test_harness_presets.py` for the drift-detector.
    dh_p = sub.add_parser(
        "detect-harness",
        help="Detect the repo's harness stack and (optionally) install canonical routines.",
        description=(
            "Identify the repo's test/build harness from filesystem "
            "hints declared by `harness_presets:` in the catalog, then "
            "either print the proposed routine set (default) or write "
            "`.iteration/config.yaml` non-interactively (with --apply). "
            "This is the express path that lets a user skip the 20-min "
            "interview when their stack is unambiguous."
        ),
    )
    dh_p.add_argument(
        "--repo", required=True,
        help="Repo root to inspect for stack hints.",
    )
    dh_p.add_argument(
        "--catalog", required=True,
        help="Path to templates/routine-catalog.yaml (source of truth).",
    )
    dh_p.add_argument(
        "--apply", action="store_true",
        help=(
            "Write `.iteration/config.yaml` under --repo with the "
            "preset's canonical archetype set. Without this flag, the "
            "subcommand only prints what it would install."
        ),
    )

    # cadence: per-routine cron override. Issue #83 (PRD #74).
    # Today's only retuning paths are `budget low|medium|high` (bulk
    # re-apply, all routines) and hand-editing the YAML. This adds
    # the per-routine slider that's been a UX gap.
    cadence_p = sub.add_parser(
        "cadence",
        help="Override one routine's cron without bumping the whole tier.",
        description=(
            "Retune a single routine's cron expression without "
            "touching the other routines. Validates the routine "
            "exists, the cron parses, and the new cron respects "
            "the current budget tier's daily-fire cap. On success, "
            "rewrites config.yaml atomically and emits an `mcp-plan:` "
            "block so the SKILL.md `Mode: cadence` flow can dispatch "
            "the MCP reschedule the same way `budget` does."
        ),
    )
    cadence_p.add_argument(
        "--config", required=True,
        help="Path to .iteration/config.yaml",
    )
    cadence_p.add_argument(
        "--routine", required=True,
        help=(
            "Routine id to retune (must exist in config.routines[]). "
            "Unknown ids fail with rc=1 and a list of valid ids."
        ),
    )
    cadence_p.add_argument(
        "--cron", required=True,
        help=(
            "New cron expression (5 fields: minute hour dom month dow). "
            "Must parse and must fit the current budget tier's daily-"
            "fire cap (low ≤ 1, medium ≤ 4, high ≤ 24, custom unlimited)."
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

# Per-tier cadence ladders for value-based recompute (issue #77).
# Ordered slowest → fastest. The recompute function picks an index
# into this list based on `value_rate`:
#
#   value_rate = useful_fires / total_fires
#              where useful = (outcome == 'ok' AND increment_signal == true)
#
#   index = round(value_rate * (len(ladder) - 1))
#
# So vr=0.0 → ladder[0] (slow end), vr=1.0 → ladder[-1] (fast end),
# vr=0.5 → middle rung. The ladders are scoped within each budget
# tier — a high-value routine on `low` budget never escapes `low`'s
# fastest rung. To go faster the user bumps tier via the `budget`
# command.
#
# `custom` is intentionally absent: recompute respects user-tuned
# crons and never touches a `custom`-tier install.
CADENCE_LADDERS: dict[str, list[tuple[str, str]]] = {
    "low": [
        ("0 9 * * 1", "Mondays 9:00 AM"),
        ("0 9 * * 1,4", "Mondays + Thursdays 9:00 AM"),
        ("0 9 * * 1-5", "weekdays 9:00 AM"),
    ],
    "medium": [
        ("0 9 * * *", "9:00 AM daily"),
        ("0 */12 * * *", "every 12 hours"),
        ("0 */6 * * *", "every 6 hours"),
    ],
    "high": [
        ("0 */6 * * *", "every 6 hours"),
        ("0 */4 * * *", "every 4 hours"),
        ("0 */2 * * *", "every 2 hours"),
        ("*/30 * * * *", "every 30 minutes"),
    ],
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


# ---------------------------------------------------------------------------
# Value-based cadence recompute (issue #77)
# ---------------------------------------------------------------------------


def _load_log_entries(log_path: str) -> list[dict]:
    """Read `.iteration/log.jsonl` and return parsed entries. Lines
    that don't parse as JSON are skipped silently — a half-written
    log line must not crash the caller. Missing file returns []."""
    p = pathlib.Path(log_path)
    if not p.is_file():
        return []
    entries: list[dict] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []
    return entries


def _value_rate(entries: list[dict]) -> Optional[float]:
    """Return the fraction of entries that count as 'useful' —
    `outcome: ok` AND `increment_signal: true`. None if the input is
    empty (no evidence to throttle on)."""
    if not entries:
        return None
    useful = 0
    for e in entries:
        if e.get("outcome") == "ok" and bool(e.get("increment_signal")):
            useful += 1
    return useful / len(entries)


def recompute_cadence(
    routines: list[dict],
    log_entries: list[dict],
    budget_tier: str,
    *,
    window: int = 20,
) -> dict[str, tuple[str, str]]:
    """Compute new cron values for routines based on value-rate.

    Inputs:
      - `routines`: the `routines:` list from config.yaml. Each entry
        must have `id` and `trigger`. Routines without `trigger.cron`
        (hook/git-hook primitives) are silently skipped.
      - `log_entries`: parsed `.iteration/log.jsonl` entries. The
        function filters and windows internally.
      - `budget_tier`: one of `{'low', 'medium', 'high', 'custom'}`.
        `custom` returns `{}` (no recompute on user-tuned configs).
      - `window`: how many recent entries per routine to consider.
        Default 20 — old data shouldn't dampen a routine that's
        since recovered.

    Returns `{routine_id: (cron, human)}` only for routines whose
    cadence should CHANGE (i.e. recomputed cron != current cron).
    Empty dict means "nothing to do" — the caller can skip the write.

    Raises `ValueError` for an unknown budget tier.

    Pure: no I/O, no `datetime.now()`. The orchestrator's CLI wrapper
    handles file reads / writes."""
    if budget_tier == "custom":
        return {}
    if budget_tier not in CADENCE_LADDERS:
        raise ValueError(
            f"unknown budget tier {budget_tier!r} — recompute_cadence "
            f"knows {sorted(CADENCE_LADDERS)} (plus 'custom' which is a "
            "no-op)"
        )
    ladder = CADENCE_LADDERS[budget_tier]
    changes: dict[str, tuple[str, str]] = {}
    for routine in routines:
        rid = routine.get("id")
        if not rid:
            continue
        trigger = routine.get("trigger") or {}
        current_cron = trigger.get("cron")
        if not current_cron:
            # hook / git-hook / loop primitives — no cron to recompute.
            continue
        # Filter log entries for this routine. Window from the END
        # (most recent) — log.jsonl is append-only and chronologically
        # ordered, so the last N are "recent".
        entries = [e for e in log_entries if e.get("routine") == rid][-window:]
        vr = _value_rate(entries)
        if vr is None:
            continue
        idx = int(round(vr * (len(ladder) - 1)))
        idx = max(0, min(len(ladder) - 1, idx))
        new_cron, new_human = ladder[idx]
        if new_cron == current_cron and new_human == (trigger.get("human") or new_human):
            # Already at the right rung — idempotent skip.
            continue
        changes[rid] = (new_cron, new_human)
    return changes


def _cli_recompute_cadence(args, out, err) -> int:
    """`auto-routines recompute-cadence` — read log + config, compute
    new crons via `recompute_cadence`, write back atomically. Honors
    the routine's budget tier from meta.budget. Reports one summary
    line per touched routine. No-op exits 0 with no writes."""
    try:
        cfg = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1
    if cfg is None:
        print(f"config is empty: {args.config}", file=err)
        return 1
    meta = (cfg or {}).get("meta", {}) or {}
    budget_tier = meta.get("budget") or "custom"
    routines = cfg.get("routines", []) or []

    # Load the log; absent file = empty list (no evidence yet).
    log_path = args.log or ".iteration/log.jsonl"
    entries = _load_log_entries(log_path)

    try:
        changes = recompute_cadence(
            routines, entries, budget_tier, window=int(args.window)
        )
    except ValueError as e:
        print(f"recompute failed: {e}", file=err)
        return 1

    if not changes:
        out.write(
            f"# recompute-cadence — no changes "
            f"(budget={budget_tier!r}, log entries={len(entries)})\n"
        )
        return 0

    # Apply changes.
    for routine in routines:
        rid = routine.get("id")
        if rid not in changes:
            continue
        cron, human = changes[rid]
        trigger = routine.setdefault("trigger", {})
        trigger["cron"] = cron
        trigger["human"] = human

    try:
        _atomic_write_yaml(args.config, cfg)
    except OSError as e:
        print(f"config write failed: {e}", file=err)
        return 1

    out.write(
        f"# recompute-cadence — {len(changes)} routine(s) retuned "
        f"(budget={budget_tier!r})\n"
    )
    for rid, (cron, human) in changes.items():
        out.write(f"  {rid}: cron={cron!r} ({human})\n")
    return 0


# ---------------------------------------------------------------------------
# Cadence CLI (issue #83) — per-routine cron override
# cron helper — small enough to inline rather than add a `croniter` dep.
# Handles the 5-field standard cron expressions we use in BUDGET_PRESETS:
#   *, N, N-M, N,M,K, */N
# Used by the cadence command's tier-cap check (issue #83).
# ---------------------------------------------------------------------------

def _cron_field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """True if `value` (already in [lo, hi]) matches a single cron
    field's expression. Recursive on comma lists."""
    if "," in field:
        return any(_cron_field_matches(part, value, lo, hi) for part in field.split(","))
    if field == "*":
        return True
    if field.startswith("*/"):
        step_s = field[2:]
        if not step_s.isdigit() or int(step_s) <= 0:
            raise ValueError(f"invalid step in cron field {field!r}")
        step = int(step_s)
        # */N counts from lo: (value - lo) divisible by step.
        return (value - lo) % step == 0
    if "-" in field:
        a_s, b_s = field.split("-", 1)
        if not (a_s.isdigit() and b_s.isdigit()):
            raise ValueError(f"invalid range in cron field {field!r}")
        a, b = int(a_s), int(b_s)
        return a <= value <= b
    if not field.isdigit():
        raise ValueError(f"invalid cron field {field!r}")
    return int(field) == value


def _cron_matches_minute(cron: str, dt: dt.datetime) -> bool:
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron must have 5 fields (minute hour dom month dow); "
            f"got {len(fields)} in {cron!r}"
        )
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    # `weekday()` returns Mon=0..Sun=6. Standard cron has Sun=0..Sat=6
    # (and many implementations also accept Sun=7). We support both:
    # cron dow=0 or 7 → Sun, dow=1..6 → Mon..Sat.
    py_dow = dt.weekday()
    cron_dow = (py_dow + 1) % 7  # Mon=1..Sun=0
    # If the field literally contains 7, also accept that as Sun.
    dow_match = _cron_field_matches(dow_f, cron_dow, 0, 7)
    if not dow_match and "7" in dow_f:
        dow_match = _cron_field_matches(dow_f, 7, 0, 7)
    return (
        _cron_field_matches(minute_f, dt.minute, 0, 59)
        and _cron_field_matches(hour_f, dt.hour, 0, 23)
        and _cron_field_matches(dom_f, dt.day, 1, 31)
        and _cron_field_matches(month_f, dt.month, 1, 12)
        and dow_match
    )


def cron_fires_per_day(cron: str) -> float:
    """Return the average number of times `cron` fires per day,
    averaged over a representative 7-day window. Used by the cadence
    command to enforce per-tier daily-fire caps.

    Raises ValueError if `cron` is malformed (wrong field count,
    garbage tokens). Exposed at module top-level so tests can pin
    behavior independently of the CLI."""
    # 2024-01-15 is a Monday; the 7-day window covers every weekday
    # so weekly schedules (Mon-Fri 9 AM) average correctly.
    base = dt.datetime(2024, 1, 15, 0, 0)
    count = 0
    for m in range(7 * 1440):
        if _cron_matches_minute(cron, base + dt.timedelta(minutes=m)):
            count += 1
    return count / 7.0


# Per-tier daily-fire cap. Chosen so the existing BUDGET_PRESETS fit
# their own tier (low's `weekdays 9 AM` = 5/7 ≈ 0.71 < 1; medium's
# `every 12 hours` = 2 < 4; high's `every 4 hours` = 6 < 24) with
# headroom for hand-tuning. `custom` is unlimited — the escape hatch.
BUDGET_TIER_DAILY_CAP: dict[str, float] = {
    "low": 1.0,
    "medium": 4.0,
    "high": 24.0,
    "custom": float("inf"),
}


def _cron_to_human(cron: str) -> str:
    """Render a 5-field cron expression to a short human label for
    the status table. Best-effort for the patterns BUDGET_PRESETS
    uses; falls back to the raw cron for anything exotic so the
    status table never displays a misleading approximation."""
    fields = cron.split()
    if len(fields) != 5:
        return cron
    minute, hour, dom, month, dow = fields
    # `0 */N * * *` → every N hours
    if minute == "0" and hour.startswith("*/") and dom == month == dow == "*":
        return f"every {hour[2:]} hours"
    # `*/N * * * *` → every N minutes
    if minute.startswith("*/") and hour == "*" and dom == month == dow == "*":
        return f"every {minute[2:]} minutes"
    # `0 H * * *` → daily at H:00
    if minute == "0" and hour.isdigit() and dom == month == dow == "*":
        h = int(hour)
        period = "AM" if h < 12 else "PM"
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        return f"{h12}:00 {period} daily"
    # `0 H * * 1-5` → weekdays at H:00
    if minute == "0" and hour.isdigit() and dom == "*" and month == "*" and dow == "1-5":
        h = int(hour)
        period = "AM" if h < 12 else "PM"
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        return f"weekdays {h12}:00 {period}"
    return cron


def _cli_cadence(args, out, err) -> int:
    """Retune a single routine's cron (issue #83).

    Validation order:
      1. Routine id exists.
      2. Cron parses.
      3. New cron fits the current budget tier's daily-fire cap.

    On success, rewrites trigger.cron + trigger.human atomically and
    emits an `mcp-plan:` block (same shape `budget` uses) so the
    skill consumer can dispatch the MCP retune.
    """
    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    if config is None:
        print(f"config is empty: {args.config}", file=err)
        return 1

    routines = config.get("routines", []) or []

    # 1. Routine must exist.
    target = next((r for r in routines if r.get("id") == args.routine), None)
    if target is None:
        valid_ids = sorted(r.get("id", "") for r in routines)
        ids_listing = ", ".join(valid_ids) if valid_ids else "(none)"
        print(
            f"cadence: no routine with id={args.routine!r}. "
            f"Valid ids: {ids_listing}",
            file=err,
        )
        return 1

    # 2. Cron must parse.
    new_cron = args.cron.strip()
    try:
        fires_per_day = cron_fires_per_day(new_cron)
    except ValueError as e:
        print(f"cadence: invalid cron {new_cron!r}: {e}", file=err)
        return 1

    # 3. Tier cap.
    tier = ((config.get("meta") or {}).get("budget") or "medium").lower()
    cap = BUDGET_TIER_DAILY_CAP.get(tier)
    if cap is None:
        # Unknown tier in config — treat as medium and warn rather
        # than reject (the cadence command's contract is per-routine,
        # not per-tier).
        cap = BUDGET_TIER_DAILY_CAP["medium"]
        print(
            f"# warn: unknown budget tier {tier!r} in config; "
            f"treating as medium (cap {cap}/day)",
            file=out,
        )
    if fires_per_day > cap:
        print(
            f"cadence: cron {new_cron!r} fires "
            f"{fires_per_day:.2f}/day, which exceeds the "
            f"{tier!r} tier cap of {cap}/day. Raise the tier first "
            f"with `python3 scripts/orchestrator.py budget --config "
            f"{args.config} --tier <higher>` (or set tier=custom to "
            f"opt out of the cap).",
            file=err,
        )
        return 1

    # All validation passed — rewrite the routine's trigger.
    before_cron = (target.get("trigger") or {}).get("cron", "(none)")
    trigger = target.setdefault("trigger", {})
    trigger["cron"] = new_cron
    trigger["human"] = _cron_to_human(new_cron)

    try:
        _atomic_write_yaml(args.config, config)
    except OSError as e:
        print(f"config write failed: {e}", file=err)
        return 1

    out.write(
        f"# cadence: routine {args.routine!r} retuned\n"
        f"# before: {before_cron}\n"
        f"# after:  {new_cron}\n"
        f"# human:  {trigger['human']}\n"
        f"# tier:   {tier} (cap {cap}/day; this cron is {fires_per_day:.2f}/day)\n"
    )

    # MCP plan emission. If the routine has a stored task_id, emit a
    # JSON line the SKILL.md consumer can pipe to
    # `mcp__scheduled-tasks__update_scheduled_task`. If not (git-hook,
    # hook, loop, pr-poll), emit a warning so the user knows the
    # YAML override isn't backed by a live MCP reschedule.
    out.write("mcp-plan:\n")
    task_id = target.get("task_id")
    if task_id:
        out.write(json.dumps({
            "routine_id": args.routine,
            "task_id": task_id,
            "cron": new_cron,
            "human": trigger["human"],
        }, sort_keys=True) + "\n")
    else:
        out.write(
            f"# warn: routine {args.routine!r} has no stored task_id "
            f"(primitive={target.get('primitive', '?')!r}); the YAML "
            f"override is documentation only — no live MCP "
            f"reschedule will happen.\n"
        )

    return 0


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


# Default stagnation threshold when neither per-routine nor meta-level
# override is set. Mirrors the value seeded by `init` in
# templates/config.yaml so a hand-edited config can omit the field
# and still get sensible behavior.
_FSM_DEFAULT_STAGNATION_THRESHOLD = 7


def _cli_fsm_plan(args, out, err) -> int:
    """Emit ACTIVE→STAGNANT transition plan lines for stagnant routines.

    Rule:  runs_since_useful = stats.runs - (stats.last_useful_iter or 0)
    Trigger: runs_since_useful >= threshold
    Threshold resolution (first that exists):
        1. routine.stagnation_threshold
        2. meta.default_stagnation_threshold
        3. _FSM_DEFAULT_STAGNATION_THRESHOLD (=7)

    Only routines with state == "ACTIVE" are candidates. Every other
    state is either already-paused (STAGNANT, COMPLETED, STOPPED),
    transient (EVOLVING), or pre-confirm (PROPOSED) — none can
    transition to STAGNANT via this rule.

    Pure read+emit: no config rewrite, no MCP, no network. The eventual
    apply step (rewrite routines[].state = "STAGNANT") is a separate
    concern shipped in a later slice."""
    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    if not isinstance(config, dict):
        print("config root must be a mapping", file=err)
        return 1

    meta = (config.get("meta") or {}) if isinstance(config.get("meta"), dict) else {}
    meta_default = meta.get("default_stagnation_threshold")
    if not isinstance(meta_default, int) or isinstance(meta_default, bool) or meta_default < 1:
        meta_default = _FSM_DEFAULT_STAGNATION_THRESHOLD

    routines = config.get("routines") or []
    if not isinstance(routines, list):
        print("config.routines must be a list", file=err)
        return 1

    plan_entries: list[dict] = []

    for r in routines:
        if not isinstance(r, dict):
            continue
        if r.get("state") != "ACTIVE":
            # Only ACTIVE routines are candidates. Everything else
            # is already paused, terminal, or transient.
            continue

        # Threshold resolution: per-routine wins, else meta default,
        # else built-in fallback.
        rt = r.get("stagnation_threshold")
        if isinstance(rt, int) and not isinstance(rt, bool) and rt >= 1:
            threshold = rt
        else:
            threshold = meta_default

        # Stats may be missing (hand-edited config) or partially
        # populated (legacy install). Defaulting to a zero-runs view
        # means a fresh routine can't be stagnant — matches intuition.
        stats = r.get("stats") if isinstance(r.get("stats"), dict) else {}
        runs = stats.get("runs", 0)
        last_useful = stats.get("last_useful_iter")

        # Coerce non-int runs to 0 — a typo in the YAML shouldn't
        # crash the whole evolve fire. (Sanity-check would have caught
        # it; we're being defensive about a config that bypassed it.)
        if not isinstance(runs, int) or isinstance(runs, bool):
            runs = 0
        baseline = (
            last_useful
            if isinstance(last_useful, int) and not isinstance(last_useful, bool)
            else 0
        )
        runs_since_useful = max(0, runs - baseline)

        if runs_since_useful >= threshold:
            plan_entries.append({
                "routine_id": r.get("id", "?"),
                "from": "ACTIVE",
                "to": "STAGNANT",
                "reason": (
                    f"{runs_since_useful} runs since last useful outcome "
                    f"(threshold={threshold})"
                ),
            })

    for entry in plan_entries:
        out.write(json.dumps(entry, sort_keys=True) + "\n")

    return 0


# Required fields on every plan line. Anything missing → refuse with a
# clear error rather than guessing — fsm-plan always emits all three,
# and a hand-edited plan that omits one is almost certainly a typo.
_APPLY_REQUIRED_PLAN_FIELDS = ("routine_id", "from", "to")


def _emit_apply_record(
    out, routine_id: str, frm: str | None, to: str | None,
    ok: bool, detail: str,
) -> None:
    """One JSON line per plan entry — matches the `install-doctor`
    output shape so downstream parsers can be uniform."""
    out.write(json.dumps({
        "routine_id": routine_id,
        "from": frm,
        "to": to,
        "ok": ok,
        "detail": detail,
    }, sort_keys=True) + "\n")


def _cli_apply_fsm_plan(args, out, err) -> int:
    """Apply FSM transitions from a JSONL plan to config.yaml.

    Pure-script write-half of SKILL.md `Mode: evolve` step 5. The
    earlier `fsm-plan` produced JSONL of `{routine_id, from, to, ...}`;
    this command consumes those lines and rewrites config.yaml so
    each routine's `state` matches the plan's `to`.

    Validation strategy: PRE-FLIGHT every line before mutating any
    state. If any line fails (unknown routine, `from` doesn't match
    the current state, malformed JSON, missing required field), emit
    failure records for the bad lines AND for any pending lines, then
    exit non-zero WITHOUT touching config.yaml. The user fixes the
    plan and re-runs.

    Why all-or-nothing: a half-applied plan leaves the user with a
    config that's neither the old nor the new FSM state. Recovery
    means hand-editing YAML, which is exactly the kind of foot-gun
    we're pulling out of the LLM prose path.

    Atomic write: tempfile + os.replace via `_atomic_write_yaml`.
    Same guarantees as every other config-mutating wrapper in this
    file."""
    # Load config first — a missing/malformed config is operator
    # error and we surface it before parsing the plan.
    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    if not isinstance(config, dict):
        print("config root must be a mapping", file=err)
        return 1

    routines = config.get("routines") or []
    if not isinstance(routines, list):
        print("config.routines must be a list", file=err)
        return 1

    # Index routines by id for O(1) lookup during validation. Preserve
    # the original list order — we mutate in place rather than rebuild.
    by_id: dict[str, dict] = {}
    for r in routines:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            by_id[r["id"]] = r

    # Read the plan. `--plan -` means stdin; otherwise a file path.
    # We tolerate blank lines and `#`-prefixed comments — `fsm-plan`
    # doesn't emit them, but a hand-edited plan might.
    try:
        if args.plan == "-":
            plan_text = sys.stdin.read()
        else:
            plan_text = pathlib.Path(args.plan).read_text(encoding="utf-8")
    except OSError as e:
        print(f"plan load failed: {e}", file=err)
        return 1

    parsed: list[tuple[int, dict]] = []  # (line_number, entry)
    parse_errors: list[tuple[int, str]] = []  # (line_number, detail)
    for i, raw in enumerate(plan_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            parse_errors.append((i, f"malformed JSON on plan line {i}: {e}"))
            continue
        if not isinstance(entry, dict):
            parse_errors.append((i, f"plan line {i} is not a JSON object"))
            continue
        parsed.append((i, entry))

    # Validate every parsed entry against the current config. We
    # accumulate per-line results so the user sees the FULL picture
    # in one apply — not "fix line 1, run again, see line 2 failed".
    validation: list[dict] = []
    any_failure = bool(parse_errors)

    for lineno, entry in parsed:
        rid = entry.get("routine_id")
        frm = entry.get("from")
        to = entry.get("to")
        missing = [f for f in _APPLY_REQUIRED_PLAN_FIELDS if not entry.get(f)]
        if missing:
            validation.append({
                "routine_id": rid or f"<line {lineno}>",
                "from": frm, "to": to,
                "ok": False,
                "detail": f"plan line {lineno} missing required field(s): {missing}",
            })
            any_failure = True
            continue

        routine = by_id.get(rid)
        if routine is None:
            validation.append({
                "routine_id": rid, "from": frm, "to": to,
                "ok": False,
                "detail": f"routine {rid!r} not found in config",
            })
            any_failure = True
            continue

        current = routine.get("state")
        if current != frm:
            validation.append({
                "routine_id": rid, "from": frm, "to": to,
                "ok": False,
                "detail": (
                    f"current state is {current!r}, plan expected "
                    f"{frm!r} — plan is stale; re-run fsm-plan and "
                    f"try again"
                ),
            })
            any_failure = True
            continue

        # Valid — would-apply.
        validation.append({
            "routine_id": rid, "from": frm, "to": to,
            "ok": True,
            "detail": "valid (pending apply)" if any_failure else "applied",
        })

    if any_failure:
        # Emit parse errors first (they don't have routine context).
        for lineno, detail in parse_errors:
            _emit_apply_record(
                out, f"<line {lineno}>", None, None, False, detail,
            )
        # Then per-routine validation results. Flip any "valid (pending
        # apply)" to ok:true detail "skipped (other transition failed)"
        # so the user sees that this transition didn't land either.
        for v in validation:
            detail = v["detail"]
            if v["ok"] and detail == "valid (pending apply)":
                v = {**v, "ok": False,
                     "detail": "skipped: another transition in this plan failed"}
            out.write(json.dumps(v, sort_keys=True) + "\n")
        return 1

    # All clear — mutate states in place and write atomically.
    for v in validation:
        routine = by_id[v["routine_id"]]
        routine["state"] = v["to"]

    try:
        _atomic_write_yaml(args.config, config)
    except OSError as e:
        print(f"config write failed: {e}", file=err)
        return 1

    for v in validation:
        out.write(json.dumps(v, sort_keys=True) + "\n")

    return 0


def _cli_verify_fsm_state(args, out, err) -> int:
    """Verify each routine's current state matches the plan's `to`.

    Read-side companion to `apply-fsm-plan`. Consumes the SAME JSONL
    plan: each line `{routine_id, to, ...}` becomes an assertion
    "routine X must currently be in state Y". The `from` field is
    ignored (it described the pre-apply state, which is no longer
    relevant once the plan has been applied).

    Output is one JSON record per assertion:
        {routine_id, expected, actual, ok, detail}
    matching the JSONL contract used by `apply-fsm-plan` and
    `install-doctor`. Exit 0 iff every assertion holds.

    A failing assertion does NOT stop processing — we evaluate every
    line and emit results for all, so the user sees the full picture
    in a single run. The exit code rolls up to 1 iff any record has
    `ok: false`.

    Pure read: never writes to config.yaml. Safe to run repeatedly,
    safe to run mid-evolve, safe to run as a cron-driven drift check
    independent of any apply step."""
    try:
        config = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    if not isinstance(config, dict):
        print("config root must be a mapping", file=err)
        return 1

    routines = config.get("routines") or []
    if not isinstance(routines, list):
        print("config.routines must be a list", file=err)
        return 1

    by_id: dict[str, dict] = {}
    for r in routines:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            by_id[r["id"]] = r

    # Plan ingestion. `--plan -` reads stdin; otherwise a file path.
    try:
        if args.plan == "-":
            plan_text = sys.stdin.read()
        else:
            plan_text = pathlib.Path(args.plan).read_text(encoding="utf-8")
    except OSError as e:
        print(f"plan load failed: {e}", file=err)
        return 1

    any_failure = False

    for i, raw in enumerate(plan_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Malformed JSON: emit a failure record AND set the exit-1
        # flag. We can't even produce a routine_id for it, so the
        # record uses a synthetic `<line N>` placeholder.
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            out.write(json.dumps({
                "routine_id": f"<line {i}>",
                "expected": None,
                "actual": None,
                "ok": False,
                "detail": f"malformed JSON on plan line {i}: {e}",
            }, sort_keys=True) + "\n")
            any_failure = True
            continue

        if not isinstance(entry, dict):
            out.write(json.dumps({
                "routine_id": f"<line {i}>",
                "expected": None,
                "actual": None,
                "ok": False,
                "detail": f"plan line {i} is not a JSON object",
            }, sort_keys=True) + "\n")
            any_failure = True
            continue

        rid = entry.get("routine_id")
        expected = entry.get("to")

        # `routine_id` and `to` are required; `from` is informational
        # for verify (we don't check it).
        if not rid or not expected:
            missing = [f for f in ("routine_id", "to") if not entry.get(f)]
            out.write(json.dumps({
                "routine_id": rid or f"<line {i}>",
                "expected": expected,
                "actual": None,
                "ok": False,
                "detail": (
                    f"plan line {i} missing required field(s): {missing}"
                ),
            }, sort_keys=True) + "\n")
            any_failure = True
            continue

        routine = by_id.get(rid)
        if routine is None:
            out.write(json.dumps({
                "routine_id": rid,
                "expected": expected,
                "actual": None,
                "ok": False,
                "detail": f"routine {rid!r} not found in config",
            }, sort_keys=True) + "\n")
            any_failure = True
            continue

        actual = routine.get("state")
        if actual == expected:
            out.write(json.dumps({
                "routine_id": rid,
                "expected": expected,
                "actual": actual,
                "ok": True,
                "detail": "state matches",
            }, sort_keys=True) + "\n")
        else:
            out.write(json.dumps({
                "routine_id": rid,
                "expected": expected,
                "actual": actual,
                "ok": False,
                "detail": (
                    f"expected state {expected!r}, found {actual!r} — "
                    f"apply step may have been skipped, partial, or "
                    f"someone hand-edited config.yaml after apply"
                ),
            }, sort_keys=True) + "\n")
            any_failure = True

    return 1 if any_failure else 0


def _cli_open_pr(args, out, err) -> int:
    """Deterministic wrapper around `gh pr create`.

    Resolves the default base via `git symbolic-ref` when --base is
    omitted, then invokes `gh pr create` with the canonical four
    flags. Emits the PR URL on stdout on success; propagates non-zero
    exits from either subprocess call.

    Critically: ALL external commands go through `subprocess.run` so
    tests can mock the call shape via `monkeypatch.setattr(subprocess,
    "run", ...)`. This is the PRD's 'Mock the gh pr create path'
    requirement made concrete — without a Python wrapper the call
    shape lived only in LLM prompt bodies, untestable from CI."""
    # Lazy import — pure-function callers don't pay the subprocess cost
    # at module load.
    import subprocess

    base = args.base
    if base is None:
        # Resolve origin's default branch. The `--short` flag returns
        # `origin/<branch>`; we strip the prefix to get just the branch.
        # If this fails (no origin, detached HEAD), short-circuit
        # BEFORE attempting gh — otherwise gh prints a misleading
        # error about the base ref.
        proc = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            stderr_text = proc.stderr or ""
            print(
                "could not resolve default base branch via "
                "`git symbolic-ref --short refs/remotes/origin/HEAD`. "
                "Pass --base explicitly, or run "
                "`git remote set-head origin --auto` to fix the "
                f"origin HEAD ref. git stderr: {stderr_text.strip()}",
                file=err,
            )
            return proc.returncode or 1
        # Strip the `origin/` prefix; mirrors the canonical bash
        # one-liner in templates/routine-preamble.md.
        base = proc.stdout.strip()
        if base.startswith("origin/"):
            base = base[len("origin/"):]

    # Assemble the canonical four-flag invocation. NO --repo — routines
    # stay in-repo by design (cross-repo opens are out of scope and an
    # attack surface). NO --draft — keep the surface minimal in this
    # slice; add it as a separate flag when actually needed.
    gh_argv = [
        "gh", "pr", "create",
        "--base", base,
        "--head", args.head,
        "--title", args.title,
        "--body", args.body,
    ]
    proc = subprocess.run(gh_argv, capture_output=True, text=True)

    # On failure, propagate stderr to the caller — silent success
    # would falsely advance routine state (e.g., logging
    # `outcome: ok, summary: <PR url>` when no PR opened).
    if proc.returncode != 0:
        stderr_text = (proc.stderr or "").strip()
        stdout_text = (proc.stdout or "").strip()
        if stderr_text:
            print(stderr_text, file=err)
        if stdout_text:
            # Some gh failures put diagnostic info on stdout — surface
            # it too, but to stderr so callers parsing stdout for the
            # URL don't pick up garbage.
            print(stdout_text, file=err)
        return proc.returncode

    # Success: gh prints the PR URL on stdout. Echo it through so
    # the caller can capture it and put it in log lines / iter-NNN.md.
    out.write(proc.stdout)
    return 0


# Canonical Markdown-table header for a fresh checkpoints.md. Mirrors
# the in-repo `.iteration/checkpoints.md` so a freshly-initialized file
# and an actively-used one share the same shape (status command,
# dashboards, future revert wrappers can rely on it).
_CHECKPOINT_HEADER = (
    "# auto-routines checkpoints\n"
    "\n"
    "Each row is a successful iter checkpoint. Append-only — never rewrite history.\n"
    "Format: `iter | YYYY-MM-DDTHH:MM:SS±ZZZZ | sha | one-line summary`\n"
    "\n"
    "| iter | when | sha | summary |\n"
    "|------|------|-----|---------|\n"
)

# Match a data row's leading iter cell. Allows leading whitespace inside
# the cell (table writers sometimes pad). Captured group is the integer.
_CHECKPOINT_ROW_ITER_RE = re.compile(r"^\|\s*(\d+)\s*\|")


def _cli_checkpoint_append(args, out, err) -> int:
    """Append a row to a Markdown-table checkpoints file.

    The contract (pinned by `tests/test_checkpoint_append.py`):
    - If the file is missing, write the canonical header + first row
      with `iter=1`.
    - If the file exists, parse out existing iter numbers, compute
      `next = max(existing) + 1` (not `count + 1` — max is idempotent
      under partial-write recovery), and append a single row.
    - Timestamp uses `strftime('%Y-%m-%dT%H:%M:%S%z')` so we get a
      local offset (`+HHMM` or `-HHMM`), never UTC `Z` — SKILL.md is
      explicit about local-machine readability.
    - Reject `|` in the summary loudly — silently writing it would
      corrupt the table.
    - Write atomically (tempfile + os.replace) so a crash mid-write
      never leaves a half-written checkpoints.md.
    - Echo the appended row on stdout so the caller can log it
      without re-reading the file.
    """
    if "|" in args.summary:
        print(
            f"summary must not contain `|` (would break the Markdown "
            f"table). Got: {args.summary!r}",
            file=err,
        )
        return 1

    target = pathlib.Path(args.file)

    # Parse existing iters if the file exists. Tolerate a missing file
    # (fresh install) without erroring — that's the first-checkpoint path.
    existing_text = ""
    existing_iters: list[int] = []
    if target.exists():
        try:
            existing_text = target.read_text()
        except OSError as e:
            print(f"could not read {target}: {e}", file=err)
            return 1
        for line in existing_text.splitlines():
            m = _CHECKPOINT_ROW_ITER_RE.match(line)
            if m:
                try:
                    existing_iters.append(int(m.group(1)))
                except ValueError:
                    # A header row like "|------|------|" won't match
                    # \d+, but defend against any other oddity.
                    continue

    next_iter = max(existing_iters) + 1 if existing_iters else 1

    # Local ISO-8601 with offset. `%z` produces `+HHMM` (not `+HH:MM`),
    # which matches the in-repo `.iteration/checkpoints.md`. The test
    # regex accepts both shapes.
    ts = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    row = f"| {next_iter} | {ts} | {args.sha} | {args.summary} |"

    # Build the new content. If the file is missing OR the table header
    # is absent, prepend the canonical header. Otherwise just append.
    if not existing_text:
        new_content = _CHECKPOINT_HEADER + row + "\n"
    else:
        # Preserve a trailing newline contract: if the existing file
        # doesn't end in `\n`, add one before the new row.
        sep = "" if existing_text.endswith("\n") else "\n"
        new_content = existing_text + sep + row + "\n"

    # Atomic write: tempfile in the same directory, then os.replace.
    # Same-dir is critical — os.replace is only atomic on the same
    # filesystem.
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"could not create parent dir {parent}: {e}", file=err)
        return 1

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(parent),
            prefix=".checkpoints.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(new_content)
            tmp_path = tmp.name
        os.replace(tmp_path, str(target))
    except OSError as e:
        print(f"could not write checkpoint to {target}: {e}", file=err)
        return 1

    out.write(row + "\n")
    return 0


# `{{name}}` placeholder pattern. Matches the SKILL.md placeholder table
# (line 712) exactly — alphanumeric + underscore only. Anything that
# survives substitution against this pattern is a wrapper bug.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# The self-evolution snippet rendered when routine.self_evolve is true.
# Single source of truth — the LLM previously rendered this from prose
# in SKILL.md, getting subtly different wording per install. Pinning
# it here makes installs byte-reproducible.
_SELF_EVOLVE_BLOCK_TRUE = (
    "If during a fire you conclude this routine's config is wrong "
    "(stale, noisy, or misaligned with the current goal), append a "
    "request to `.iteration/evolve_requests.jsonl` with one JSON "
    "object per line:\n"
    "\n"
    "```json\n"
    '{"ts": "<local-iso-with-offset>", "routine": "<your-routine-id>", '
    '"kind": "config-change", "reason": "<one-line why>", '
    '"proposal": "<one-line what>"}\n'
    "```\n"
    "\n"
    "The meta-agent drains this on its next fire (see "
    "`.claude/skills/_shared/preamble.md` for the canonical contract)."
)

_SELF_EVOLVE_BLOCK_FALSE = (
    "Self-evolution is disabled for this routine "
    "(`routine.self_evolve: false`). Do not attempt to mutate your "
    "own config from inside a fire — the meta-agent will not process "
    "requests from routines with self_evolve off."
)


def _cli_render_routine_skill(args, out, err) -> int:
    """Deterministic placeholder substitution for per-routine SKILL.md.

    The PRD requires installed SKILL.md files contain "no
    `{{placeholders}}`". Previously this was a pure-LLM rendering
    step (SKILL.md install step 6f) — and the LLM would fat-finger
    placeholders (leftover `{{routine_id}}`, UTC `Z` instead of local
    offset, prompt_body pulled from config instead of catalog).

    This wrapper does the substitution mechanically:
    - routine_id, purpose, primitive, iter_added: from config.yaml
      routine entry
    - prompt_body: from catalog.yaml archetype (config never has it —
      would bloat the file)
    - trigger_summary: from routine.trigger.human
    - success_criterion: from routine.success_criterion with fallback
      to `(none — runs indefinitely)`
    - installed_at: --installed-at arg if given, else now() with local
      offset (never `Z`)
    - self_evolve_block: branches on routine.self_evolve
    - routine_specific_inputs: empty string by default; catalog can
      override via `routine_specific_inputs:` field

    After substitution, any remaining `{{...}}` is a hard error — we
    refuse to write a half-rendered file. Atomic write via tempfile +
    os.replace, so a failed render leaves no partial file behind.
    """
    # Lazy imports — keeps the pure-function path import-free for
    # callers that don't need rendering.
    try:
        cfg = _load_yaml(args.config)
    except (OSError, Exception) as e:
        print(f"config load failed: {e}", file=err)
        return 1

    routines = (cfg or {}).get("routines", []) or []
    routine = next((r for r in routines if r.get("id") == args.routine), None)
    if routine is None:
        known = ", ".join(r.get("id", "?") for r in routines) or "(none)"
        print(
            f"unknown routine id: {args.routine!r} "
            f"(known routines: {known})",
            file=err,
        )
        return 1

    try:
        catalog = _load_yaml(args.catalog)
    except (OSError, Exception) as e:
        print(f"catalog load failed: {e}", file=err)
        return 1

    archetypes = (catalog or {}).get("archetypes", []) or []
    # Archetype is keyed by routine.id — the contract is "config and
    # catalog align by id". A `prompt_skill` field for aliasing was
    # considered and dropped: in practice every config sets
    # `prompt_skill: <same as id>`, so the alias adds no value and
    # would let a typo silently route to the wrong archetype.
    archetype_id = routine.get("id")
    archetype = next(
        (a for a in archetypes if a.get("id") == archetype_id), None
    )
    if archetype is None:
        print(
            f"no catalog archetype matches routine "
            f"{args.routine!r} (looked for archetype id "
            f"{archetype_id!r}). Cannot render: prompt_body has nowhere "
            f"to come from.",
            file=err,
        )
        return 1

    prompt_body = archetype.get("prompt_body") or ""
    if not prompt_body.strip():
        print(
            f"catalog archetype {archetype_id!r} has empty prompt_body — "
            f"would render a SKILL.md with no `What to do` section. "
            f"Refusing.",
            file=err,
        )
        return 1

    try:
        template = pathlib.Path(args.template).read_text(encoding="utf-8")
    except OSError as e:
        print(f"template load failed: {e}", file=err)
        return 1

    # Build the substitution map. None values become empty strings —
    # the unknown-placeholder check below catches anything we forgot.
    success = routine.get("success_criterion") or ""
    if not success.strip():
        success = "(none — runs indefinitely)"

    trigger = routine.get("trigger", {}) or {}
    trigger_summary = trigger.get("human") or trigger.get("cron") or "(no trigger)"

    if args.installed_at:
        installed_at = args.installed_at
    else:
        # Local-now with offset. SKILL.md is explicit: never UTC `Z`.
        installed_at = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    self_evolve = bool(routine.get("self_evolve", False))
    self_evolve_block = (
        _SELF_EVOLVE_BLOCK_TRUE if self_evolve else _SELF_EVOLVE_BLOCK_FALSE
    )

    # routine_specific_inputs: catalog may override with a curated bullet
    # list (PR routines get `gh pr list`, CI routines get `gh run list`).
    # Default to empty string when absent — the template's bullet list
    # already includes the universal inputs above the placeholder.
    routine_specific_inputs = archetype.get("routine_specific_inputs") or ""

    substitutions = {
        "routine_id": str(routine.get("id", "")),
        "purpose": str(routine.get("purpose", "")),
        "primitive": str(routine.get("primitive", "")),
        "iter_added": str(routine.get("iter_added", "")),
        "trigger_summary": str(trigger_summary),
        "success_criterion": str(success),
        "installed_at": str(installed_at),
        "self_evolve_block": str(self_evolve_block),
        "routine_specific_inputs": str(routine_specific_inputs),
        "routine_prompt_body": str(prompt_body),
    }

    def _sub(m: "re.Match") -> str:
        name = m.group(1)
        if name not in substitutions:
            # Don't substitute — leave the literal in place so the
            # post-render check catches it and fails loudly. We collect
            # unknown names below to surface them all at once.
            return m.group(0)
        return substitutions[name]

    rendered = _PLACEHOLDER_RE.sub(_sub, template)

    # Post-render check: any `{{...}}` left over means either the
    # template has a placeholder we don't know about, or our
    # substitution introduced one (it shouldn't — prompt_body is
    # treated as a literal). Refuse loudly.
    leftover = _PLACEHOLDER_RE.findall(rendered)
    if leftover:
        unique_names = sorted(set(leftover))
        print(
            f"render-routine-skill: refusing to write {args.out!r}; "
            f"unknown placeholder(s) survived substitution: "
            f"{unique_names}. Either the template references a variable "
            f"the wrapper doesn't know how to fill, or the catalog's "
            f"prompt_body contains literal `{{{{...}}}}` syntax (it "
            f"shouldn't — only the template uses placeholders).",
            file=err,
        )
        return 1

    # Atomic write. Same-dir tempfile so os.replace is on the same
    # filesystem (atomicity guarantee).
    target = pathlib.Path(args.out)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"could not create parent dir {parent}: {e}", file=err)
        return 1

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(parent),
            prefix=".routine-skill.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(rendered)
            tmp_path = tmp.name
        os.replace(tmp_path, str(target))
    except OSError as e:
        print(f"could not write rendered SKILL.md to {target}: {e}", file=err)
        return 1

    # Summary line so callers (install flow) can log what got written
    # without re-reading the file. Keep it short — the file itself is
    # the artifact.
    print(
        f"rendered routine {args.routine!r} → {target} "
        f"({len(rendered)} bytes, no placeholders remain)",
        file=out,
    )
    return 0


# Routine primitives that require a working .git/hooks/post-commit
# dispatch entry. Adding a new primitive that fires on commit means
# adding it here AND updating templates/post-commit-hook.sh.
_POST_COMMIT_PRIMITIVES = frozenset({"git-hook"})


def _emit_check(out, name: str, ok: bool, detail: str) -> None:
    """Emit one install-doctor check record as a single JSON line.
    Centralized so the shape is impossible to drift across the various
    check functions."""
    json.dump(
        {"check": name, "ok": bool(ok), "detail": detail},
        out,
        sort_keys=True,
    )
    out.write("\n")


def _cli_install_doctor(args, out, err) -> int:
    """Audit a repo for a healthy auto-routines install.

    The checks performed (each emits one JSON line):
    - `config-yaml`: `.iteration/config.yaml` exists and parses.
    - `preamble`: `.claude/skills/_shared/preamble.md` exists (the
       shared contract every routine SKILL.md references).
    - `routine-skill:<id>` (one per routine): the rendered SKILL.md
       exists and contains no `{{...}}` placeholders.
    - `post-commit-hook`: if any routine has `primitive: git-hook`,
       `.git/hooks/post-commit` must exist AND be executable. If no
       git-hook routine exists, this check is `ok=true, detail='n/a'`
       (auditing transparency — caller sees the check happened).

    Exit 0 if every check is `ok`; exit 1 otherwise. The output
    contract is JSONL, one record per check — callers (the future
    `/auto-routines doctor` slash command, the dashboard, the full
    integration test) parse and report from it.
    """
    repo_root = pathlib.Path(args.repo_root)
    failures = 0

    # --- config-yaml check -------------------------------------------------
    config_path = repo_root / ".iteration" / "config.yaml"
    config_data: dict | None = None
    if not config_path.exists():
        _emit_check(
            out, "config-yaml", False,
            f".iteration/config.yaml not found at {config_path}. "
            f"Without config, no routine layout is known — re-run "
            f"`/auto-routines init`.",
        )
        failures += 1
    else:
        try:
            config_data = _load_yaml(str(config_path))
            _emit_check(
                out, "config-yaml", True,
                f".iteration/config.yaml parses ({len(config_data.get('routines', []) or [])} routines)",
            )
        except Exception as e:
            _emit_check(
                out, "config-yaml", False,
                f".iteration/config.yaml exists but does not parse "
                f"as YAML: {e}",
            )
            failures += 1
            config_data = None

    # --- preamble check ----------------------------------------------------
    preamble_path = repo_root / ".claude" / "skills" / "_shared" / "preamble.md"
    if not preamble_path.exists():
        _emit_check(
            out, "preamble", False,
            f"shared preamble missing at {preamble_path}. Every routine "
            f"SKILL.md's `## Reference` section points at this file; "
            f"without it, routines fire with no shared rules. SKILL.md "
            f"step 6f renders it from templates/routine-preamble.md.",
        )
        failures += 1
    else:
        _emit_check(
            out, "preamble", True,
            f"shared preamble present ({preamble_path.stat().st_size} bytes)",
        )

    # --- per-routine SKILL.md checks --------------------------------------
    routines = (config_data or {}).get("routines", []) or []
    for routine in routines:
        rid = routine.get("id")
        if not rid:
            # Defensive — a config with no-id routines would already
            # fail sanity-check, but we don't want to crash here.
            continue
        skill_path = repo_root / ".claude" / "skills" / rid / "SKILL.md"
        check_name = f"routine-skill:{rid}"
        if not skill_path.exists():
            _emit_check(
                out, check_name, False,
                f"per-routine SKILL.md missing at {skill_path}. The "
                f"slash command `/{rid}` won't resolve without it. "
                f"Re-render via `scripts/orchestrator.py "
                f"render-routine-skill --routine {rid} ...`.",
            )
            failures += 1
            continue
        text = skill_path.read_text(encoding="utf-8")
        leftover = _PLACEHOLDER_RE.findall(text)
        if leftover:
            unique = sorted(set(leftover))
            _emit_check(
                out, check_name, False,
                f"per-routine SKILL.md at {skill_path} contains "
                f"unsubstituted placeholder(s): {unique}. The render "
                f"wrapper should have refused to write this — "
                f"investigate how it got past PR #57's check.",
            )
            failures += 1
        else:
            _emit_check(
                out, check_name, True,
                f"rendered cleanly ({len(text)} bytes, no placeholders)",
            )

    # --- post-commit hook check (only when a git-hook routine exists) -----
    needs_post_commit = any(
        r.get("primitive") in _POST_COMMIT_PRIMITIVES for r in routines
    )
    post_commit_path = repo_root / ".git" / "hooks" / "post-commit"
    if not needs_post_commit:
        _emit_check(
            out, "post-commit-hook", True,
            "n/a — no git-hook routine in config; post-commit "
            "dispatch is not required for this install.",
        )
    elif not post_commit_path.exists():
        _emit_check(
            out, "post-commit-hook", False,
            f".git/hooks/post-commit missing at {post_commit_path}. "
            f"A git-hook routine in config will never fire — git "
            f"silently skips a non-existent hook. Re-render via "
            f"templates/post-commit-hook.sh (SKILL.md step 6c, "
            f"git-hook primitive branch).",
        )
        failures += 1
    else:
        # The hook MUST be executable. Git's behavior on a
        # non-executable hook is to silently skip it — worse than
        # a missing one, because the user sees the file and assumes
        # it works.
        mode = post_commit_path.stat().st_mode
        is_exec = bool(mode & stat.S_IXUSR) or bool(mode & stat.S_IXGRP) or bool(mode & stat.S_IXOTH)
        if not is_exec:
            _emit_check(
                out, "post-commit-hook", False,
                f".git/hooks/post-commit exists at {post_commit_path} "
                f"but is not executable (mode {oct(mode & 0o777)}). "
                f"Git will silently skip it — `chmod +x` the file or "
                f"re-render via the install procedure.",
            )
            failures += 1
        else:
            _emit_check(
                out, "post-commit-hook", True,
                f"executable hook present at {post_commit_path} "
                f"(mode {oct(mode & 0o777)})",
            )

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# detect-harness CLI (issue #78) — express install path.
# ---------------------------------------------------------------------------


# Minimal human-trigger → cron lookup for the express install. Only
# covers the phrases that ship in `templates/routine-catalog.yaml`. If a
# new archetype lands with an unfamiliar phrase, the CLI falls back to a
# safe daily-9am cron rather than guessing wrong — and a test in
# `tests/test_harness_presets.py` keeps the preset archetype set pinned
# to the catalog so this dict stays small.
_TRIGGER_HUMAN_TO_CRON: dict[str, str] = {
    "every 4 hours": "0 */4 * * *",
    "every 6 hours": "0 */6 * * *",
    "every 12 hours": "0 */12 * * *",
    "every 30 minutes": "*/30 * * * *",
    "every 15 minutes": "*/15 * * * *",
    "6:00 PM daily": "0 18 * * *",
    "9:00 AM Mondays": "0 9 * * 1",
    "9:00 AM daily": "0 9 * * *",
    "5:00 PM Mondays": "0 17 * * 1",
    "5:00 PM weekdays": "0 17 * * 1-5",
}
_TRIGGER_DEFAULT_FALLBACK_CRON = "0 9 * * *"  # safe daily 9 AM


def _routine_from_archetype(archetype: dict) -> dict:
    """Build a config.yaml `routines[]` entry from a catalog archetype.

    Schema 4 minimal — every field sanity-check requires is filled in.
    The values come straight from the archetype's defaults; the user
    can edit afterwards. Pure: no I/O."""
    primitive = archetype.get("primitive", "scheduled")
    trigger_human = archetype.get("trigger_default", "") or ""
    trigger: dict = {"human": trigger_human}
    if primitive in {"scheduled", "pr-poll"}:
        cron = _TRIGGER_HUMAN_TO_CRON.get(
            trigger_human, _TRIGGER_DEFAULT_FALLBACK_CRON,
        )
        trigger["cron"] = cron
    entry: dict = {
        "id": archetype.get("id"),
        "state": "PROPOSED",
        "enabled": True,
        "primitive": primitive,
        "est_minutes": 5,
        "trigger": trigger,
        "purpose": archetype.get("purpose", ""),
        "success_criterion": archetype.get("success_criterion", ""),
        "stagnation_threshold": 5,
        "self_evolve": bool(archetype.get("self_evolve", False)),
        "automation_level": archetype.get("automation_default", "auto"),
        "prompt_skill": archetype.get("id"),
        "iter_added": 1,
        "task_id": "",
        "last_outcome_summary": "",
        "stats": {
            "runs": 0, "useful": 0, "noisy": 0, "last_useful_iter": None,
        },
    }
    if primitive in {"scheduled", "pr-poll"}:
        entry["execution_surface"] = "local"
    return entry


def _build_express_config(repo_path: str, preset: dict, catalog: dict) -> dict:
    """Construct a minimal schema-4 config dict for `--apply` mode.

    `repo_slug` is derived from the basename of the repo path so we
    don't depend on git remotes or cwd. The user can edit afterwards."""
    import os as _os
    slug = _os.path.basename(_os.path.abspath(repo_path)) or "repo"
    # Kebab-coerce: lowercase, replace anything non-[a-z0-9-] with '-',
    # collapse runs. Sanity-check requires `^[a-z][a-z0-9]*(-[a-z0-9]+)*$`.
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "repo"
    if not slug[0].isalpha():
        slug = "repo-" + slug
    slug = slug[:32]

    archetype_by_id = {a.get("id"): a for a in (catalog.get("archetypes") or [])}
    routines: list[dict] = []
    for arch_id in preset.get("archetypes", []):
        arch = archetype_by_id.get(arch_id)
        if arch is None:
            # Drift detector should have prevented this — but if a preset
            # somehow references a phantom id, skip rather than crash.
            continue
        routines.append(_routine_from_archetype(arch))

    return {
        "schema_version": 4,
        "repo_slug": slug,
        "goal": f"Auto-installed via detect-harness ({preset.get('id')}).",
        "mode": "goal-driven",
        "created_at": _local_iso_with_offset(
            dt.datetime.now().astimezone()
        ),
        "last_iter": 1,
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": routines,
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 7,
            "default_stagnation_threshold": 5,
            "process_evolve_requests": True,
            "budget": "medium",
            "idle_window": "always",
            "gha_minutes_cap": 1500,
            "kill_switch": False,
        },
    }


def _cli_detect_harness(args, out, err) -> int:
    """Detect the repo's harness stack and (optionally) install routines.

    Without `--apply`: print the matched preset id + the archetype set
    that *would* be installed. Exit 1 if no preset matches so a shell
    wrapper can branch on the failure cleanly.

    With `--apply`: write `.iteration/config.yaml` populated from the
    preset's archetype set (each archetype mapped to a routine entry
    via `_routine_from_archetype`). Atomic via `_atomic_write_yaml`."""
    try:
        catalog = _load_yaml(args.catalog)
    except (OSError, Exception) as e:
        print(f"catalog load failed: {e}", file=err)
        return 1

    presets = (catalog or {}).get("harness_presets") or []
    if not presets:
        print(
            "catalog has no `harness_presets:` block — "
            "nothing to detect.",
            file=err,
        )
        return 1

    preset = detect_harness(args.repo, presets)
    if preset is None:
        # Non-zero so a shell wrapper sees the failure. Message goes to
        # stderr so stdout stays parseable on success.
        print(
            f"no preset matched repo at {args.repo!r}. "
            f"Stacks checked: "
            f"{', '.join(p.get('id', '?') for p in presets)}. "
            "Run the full interview (`/auto-routines init`) instead.",
            file=err,
        )
        return 1

    # Always print the detected preset + archetype set, with or without
    # --apply. With --apply the same lines act as a confirmation that
    # this is what landed in config.yaml.
    print(f"stack: {preset.get('id')}", file=out)
    name = preset.get("name")
    if name:
        print(f"name: {name}", file=out)
    print("archetypes:", file=out)
    for arch_id in preset.get("archetypes", []):
        print(f"  - {arch_id}", file=out)

    if not args.apply:
        print(
            "\nDry run — pass `--apply` to write "
            ".iteration/config.yaml non-interactively.",
            file=out,
        )
        return 0

    config_dir = pathlib.Path(args.repo) / ".iteration"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    try:
        config = _build_express_config(args.repo, preset, catalog)
        _atomic_write_yaml(str(config_path), config)
    except OSError as e:
        print(f"config write failed: {e}", file=err)
        return 1

    print(
        f"\nWrote {config_path} with "
        f"{len(config.get('routines', []))} routines. "
        "Edit before firing if you want to tune cadences.",
        file=out,
    )
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

    if args.command == "recompute-cadence":
        return _cli_recompute_cadence(args, out, err)

    if args.command == "cadence":
        return _cli_cadence(args, out, err)

    if args.command == "first-pr-eta":
        return _cli_first_pr_eta(args, out, err)

    if args.command == "drain-evolve-requests":
        return _cli_drain_evolve_requests(args, out, err)

    if args.command == "fsm-plan":
        return _cli_fsm_plan(args, out, err)

    if args.command == "apply-fsm-plan":
        return _cli_apply_fsm_plan(args, out, err)

    if args.command == "verify-fsm-state":
        return _cli_verify_fsm_state(args, out, err)

    if args.command == "open-pr":
        return _cli_open_pr(args, out, err)

    if args.command == "checkpoint-append":
        return _cli_checkpoint_append(args, out, err)

    if args.command == "render-routine-skill":
        return _cli_render_routine_skill(args, out, err)

    if args.command == "install-doctor":
        return _cli_install_doctor(args, out, err)

    if args.command == "detect-harness":
        return _cli_detect_harness(args, out, err)

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
