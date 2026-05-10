#!/usr/bin/env python3
"""
sanity-check.py — validate a proposed .iteration/config.yaml before it is applied.

Exit codes:
  0  = pass (safe to apply)
  1  = fail (do NOT apply; report printed to stdout)
  2  = bad invocation

Usage:
  python3 scripts/sanity-check.py path/to/config.yaml
  python3 scripts/sanity-check.py --stdin < config.yaml

Checks performed:
  1. YAML parses and has required top-level keys
  2. mode is one of {goal-driven, fully-auto}
  3. Every routine has: id, primitive, trigger, purpose, automation_level
  4. Routine ids are unique, kebab-case
  5. primitive is one of {hook, scheduled, loop, pr-poll}
  6. Cron strings (if any) parse to 5 fields with valid ranges
  7. automation_level in {off, notify, suggest, auto}
  8. No two scheduled routines share the exact same cron (collision warning)
  9. deps.mcps is a list of strings; deps.gh in {required, optional, none}
 10. meta.cron is present and valid
 11. anti-flap: no removed-routine id is being re-added within meta.anti_flap_window iters
     (informational — checked against history/ if present)

Designed to run with only the standard library so the skill works on any machine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml  # PyYAML — most dev machines have it
except ImportError:
    yaml = None

try:
    # Python 3.9+ stdlib — needed to validate meta.idle_window_tz against
    # the system tz database (no third-party dep, the SKILL stays portable).
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — Python ≤ 3.8 not supported by this skill
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
PRIMITIVES = {"hook", "scheduled", "loop", "pr-poll", "git-hook"}
LEVELS = {"off", "notify", "suggest", "auto"}
MODES = {"goal-driven", "fully-auto"}
GH_VALUES = {"required", "optional", "none"}
BUDGET_TIERS = {"low", "medium", "high", "custom"}
# Schema 4 — every scheduled/pr-poll routine fires on exactly one surface.
# 'both' was rejected during PRD #10 review (it created a dual-writer race
# on state.json). Hook and git-hook routines run inside the user's session
# and don't need this field.
EXECUTION_SURFACES = {"gha", "local"}
# Schema 4 — idle_window can be a clock range or the literal "always".
IDLE_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
SURFACE_REQUIRING_PRIMITIVES = {"scheduled", "pr-poll"}
HOOK_EVENTS = {
    "PreToolUse", "PostToolUse", "Notification", "Stop", "SubagentStop",
    "UserPromptSubmit", "PreCompact", "SessionStart", "SessionEnd",
}
# Finite state machine for routines.
# Active firing states:    ACTIVE, EVOLVING (transient)
# Re-openable paused:      STAGNANT, COMPLETED
# Pre-confirm:             PROPOSED
# Real terminal:           STOPPED
ROUTINE_STATES = {
    "PROPOSED", "ACTIVE", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED",
}
TERMINAL_STATES = {"STOPPED"}                              # cannot leave
PAUSED_STATES = {"STAGNANT", "COMPLETED"}                  # re-openable
FIRING_STATES = {"ACTIVE", "EVOLVING"}                     # routine may fire
SLUG_MAX = 32          # keep room for "auto-routines-" prefix and "-<routine_id>" suffix
TASK_ID_MAX = 100      # MCP-side reasonable bound

CRON_RANGES = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day-of-month
    (1, 12),   # month
    (0, 7),    # day-of-week (0 and 7 both = Sunday)
]


def parse_yaml(text: str) -> dict:
    if yaml is not None:
        return yaml.safe_load(text)
    # Minimal fallback: only supports the subset we generate. Bail loudly otherwise.
    raise RuntimeError(
        "PyYAML not installed. Run `pip install pyyaml` (or use a venv). "
        "sanity-check requires it to validate config.yaml."
    )


def idle_window_ok(value) -> bool:
    """An idle_window is either the string 'always' (never idle — work any
    time) or a clock range 'HH:MM-HH:MM' (24-hour, may wrap midnight).
    Other types or malformed strings are rejected."""
    if not isinstance(value, str):
        return False
    if value == "always":
        return True
    return bool(IDLE_WINDOW_RE.match(value))


def iana_tz_ok(value) -> bool:
    """Validate against the system tz database. PRD #10 review specifically
    flagged silent UTC fallback as a footgun — we require an IANA name so
    the orchestrator's idle-window math is unambiguous."""
    if not isinstance(value, str) or not value:
        return False
    if ZoneInfo is None:  # pragma: no cover — Python ≤ 3.8
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def cron_field_ok(field: str, lo: int, hi: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step >= 1 and step <= hi
        except ValueError:
            return False
    if "," in field:
        return all(cron_field_ok(p, lo, hi) for p in field.split(","))
    if "-" in field and "/" not in field:
        try:
            a, b = field.split("-")
            return lo <= int(a) <= int(b) <= hi
        except ValueError:
            return False
    if "/" in field:
        base, step = field.split("/", 1)
        return cron_field_ok(base, lo, hi) and step.isdigit() and int(step) >= 1
    return field.isdigit() and lo <= int(field) <= hi


def cron_ok(expr: str) -> tuple[bool, str]:
    parts = expr.split()
    if len(parts) != 5:
        return False, f"cron must have 5 fields, got {len(parts)}: {expr!r}"
    for i, (p, (lo, hi)) in enumerate(zip(parts, CRON_RANGES)):
        if not cron_field_ok(p, lo, hi):
            return False, f"cron field {i+1} invalid: {p!r} (range {lo}..{hi})"
    return True, ""


def check(config: dict) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []

    # 1. required keys
    for k in ("schema_version", "repo_slug", "goal", "mode", "deps", "routines", "meta"):
        if k not in config:
            errors.append(f"missing top-level key: {k}")

    if errors:
        return errors  # don't bother with the rest

    # 2. mode
    if config["mode"] not in MODES:
        errors.append(f"mode must be one of {MODES}, got {config['mode']!r}")

    # 2b. repo_slug must be kebab-case and bounded so the resulting MCP taskId stays sane.
    slug = config.get("repo_slug", "")
    if not isinstance(slug, str) or not KEBAB.match(slug):
        errors.append(
            f"repo_slug must be a kebab-case string (see SKILL.md guardrail 9 for the normalization rule), got {slug!r}"
        )
    elif len(slug) > SLUG_MAX:
        errors.append(
            f"repo_slug must be <= {SLUG_MAX} chars (got {len(slug)}); SKILL.md guardrail 9 requires truncation."
        )
    else:
        # taskId format is `auto-routines-<slug>-<routine_id>` (post-MCP sanitization).
        longest = max(
            (len(r.get("id", "")) for r in config.get("routines", [])),
            default=4,  # "meta"
        )
        full = len("auto-routines-") + len(slug) + 1 + max(longest, 4)
        if full > TASK_ID_MAX:
            errors.append(
                f"scheduled-task taskId would exceed {TASK_ID_MAX} chars "
                f"(slug={slug!r}, longest routine id={longest}). Shorten repo_slug or routine ids."
            )

    # 9. deps
    deps = config.get("deps", {})
    if deps.get("gh") not in GH_VALUES:
        errors.append(f"deps.gh must be one of {GH_VALUES}, got {deps.get('gh')!r}")
    mcps = deps.get("mcps", [])
    if not isinstance(mcps, list) or not all(isinstance(x, str) for x in mcps):
        errors.append("deps.mcps must be a list of strings")

    # 3-7. routines
    seen_ids: set[str] = set()
    cron_to_id: dict[str, str] = {}
    for i, r in enumerate(config.get("routines", [])):
        prefix = f"routines[{i}]"
        for k in ("id", "primitive", "trigger", "purpose", "automation_level"):
            if k not in r:
                errors.append(f"{prefix} missing key: {k}")
        rid = r.get("id", "")
        if rid:
            if rid in seen_ids:
                errors.append(f"{prefix} duplicate id: {rid}")
            seen_ids.add(rid)
            if not KEBAB.match(rid):
                errors.append(f"{prefix} id not kebab-case: {rid!r}")
            if rid in {"__meta__", "meta"}:
                errors.append(f"{prefix} id {rid!r} is reserved for the meta-routine")
        prim = r.get("primitive")
        if prim and prim not in PRIMITIVES:
            errors.append(f"{prefix} primitive must be one of {PRIMITIVES}, got {prim!r}")
        if r.get("automation_level") and r["automation_level"] not in LEVELS:
            errors.append(f"{prefix} automation_level must be one of {LEVELS}")
        # FSM state — required from schema_version 3 onward
        if "state" in r:
            if r["state"] not in ROUTINE_STATES:
                errors.append(
                    f"{prefix} state must be one of {sorted(ROUTINE_STATES)}, got {r['state']!r}"
                )
        elif config.get("schema_version", 0) >= 3:
            errors.append(f"{prefix} missing key: state (required from schema_version 3)")
        # self_evolve flag — gates mid-run /evolve requests
        if "self_evolve" in r and not isinstance(r["self_evolve"], bool):
            errors.append(f"{prefix} self_evolve must be a bool")
        # stagnation_threshold — must be a positive int when set
        if "stagnation_threshold" in r:
            st = r["stagnation_threshold"]
            if not isinstance(st, int) or st < 1:
                errors.append(f"{prefix} stagnation_threshold must be a positive integer")
        # execution_surface (schema 4+): which surface this routine fires on.
        # Only scheduled/pr-poll need it — hook/git-hook always run in-session.
        if "execution_surface" in r:
            es = r["execution_surface"]
            if not isinstance(es, str) or es not in EXECUTION_SURFACES:
                errors.append(
                    f"{prefix} execution_surface must be one of "
                    f"{sorted(EXECUTION_SURFACES)}, got {es!r}"
                )
        elif (
            config.get("schema_version", 0) >= 4
            and prim in SURFACE_REQUIRING_PRIMITIVES
        ):
            errors.append(
                f"{prefix} missing key: execution_surface "
                f"(required from schema_version 4 for primitive {prim!r})"
            )
        # est_minutes (schema 4+): orchestrator projects this against
        # meta.gha_minutes_cap before dispatching.
        if "est_minutes" in r:
            em = r["est_minutes"]
            if not isinstance(em, int) or isinstance(em, bool) or em < 1:
                errors.append(f"{prefix} est_minutes must be a positive integer")
        # 6. trigger fields per primitive
        trig = r.get("trigger", {}) or {}
        # human-readable schedule must be present when cron is (schema 3+)
        if config.get("schema_version", 0) >= 3 and "cron" in trig and "human" not in trig:
            errors.append(
                f"{prefix} trigger.human is required when trigger.cron is set "
                f"(schema 3+). Pair the cron {trig['cron']!r} with a phrase like 'every 30 minutes'."
            )
        if "human" in trig and not isinstance(trig["human"], str):
            errors.append(f"{prefix} trigger.human must be a string")
        if prim in {"scheduled", "pr-poll"}:
            if "cron" not in trig:
                errors.append(f"{prefix} primitive {prim!r} requires trigger.cron")
            else:
                ok, msg = cron_ok(trig["cron"])
                if not ok:
                    errors.append(f"{prefix} {msg}")
                else:
                    # 8. collision
                    key = trig["cron"]
                    if key in cron_to_id:
                        warnings.append(
                            f"cron collision: {rid!r} and {cron_to_id[key]!r} both run on {key!r}"
                        )
                    else:
                        cron_to_id[key] = rid
        elif prim == "hook":
            ev = trig.get("event")
            if not ev:
                errors.append(f"{prefix} primitive 'hook' requires trigger.event")
            elif ev not in HOOK_EVENTS:
                errors.append(
                    f"{prefix} trigger.event must be a Claude Code hook event "
                    f"({sorted(HOOK_EVENTS)}); got {ev!r}. Note: there is no on-commit hook event — "
                    f"use primitive 'git-hook' instead."
                )
        elif prim == "git-hook":
            # post-commit shell hook; no fields required, but cron should be absent
            if "cron" in trig:
                warnings.append(f"{prefix} git-hook ignores trigger.cron (always fires on real git commit)")
        elif prim == "loop":
            if "cron" in trig:
                warnings.append(f"{prefix} loop ignores trigger.cron (manually invoked or driven by another routine)")

    # 10. meta
    meta = config.get("meta", {})
    if "cron" not in meta:
        errors.append("meta.cron is required (the daily evolve schedule)")
    else:
        ok, msg = cron_ok(meta["cron"])
        if not ok:
            errors.append(f"meta.cron invalid: {msg}")
    if "anti_flap_window" in meta and not isinstance(meta["anti_flap_window"], int):
        errors.append("meta.anti_flap_window must be an integer")
    if "default_stagnation_threshold" in meta:
        ds = meta["default_stagnation_threshold"]
        if not isinstance(ds, int) or ds < 1:
            errors.append("meta.default_stagnation_threshold must be a positive integer")
    if "process_evolve_requests" in meta and not isinstance(meta["process_evolve_requests"], bool):
        errors.append("meta.process_evolve_requests must be a bool")
    if "budget" in meta and meta["budget"] not in BUDGET_TIERS:
        errors.append(
            f"meta.budget must be one of {sorted(BUDGET_TIERS)}, got {meta['budget']!r}. "
            f"This controls how often LLM-spawning routines fire — see SKILL.md "
            f"\"Budget → cadence presets\"."
        )
    if config.get("schema_version", 0) >= 3 and "human" not in meta:
        errors.append(
            "meta.human is required (schema 3+). e.g. '9:00 AM daily' to pair with meta.cron."
        )

    # Schema 4 — adaptive responsiveness + GHA cost ceiling
    schema_v = config.get("schema_version", 0)
    if "idle_window" in meta:
        if not idle_window_ok(meta["idle_window"]):
            errors.append(
                f"meta.idle_window must be 'always' or 'HH:MM-HH:MM' (24h), "
                f"got {meta['idle_window']!r}"
            )
    elif schema_v >= 4:
        errors.append(
            "meta.idle_window is required (schema 4+). Use 'HH:MM-HH:MM' "
            "for a quiet window or 'always' to opt out."
        )
    # tz is mandatory whenever idle_window is a real time range; optional
    # only when idle_window == 'always' (no clock math needed).
    if meta.get("idle_window") not in (None, "always"):
        if "idle_window_tz" not in meta:
            errors.append(
                "meta.idle_window_tz is required when meta.idle_window is a "
                "time range. Use an IANA zone name (e.g. 'America/Los_Angeles')."
            )
    if "idle_window_tz" in meta and not iana_tz_ok(meta["idle_window_tz"]):
        errors.append(
            f"meta.idle_window_tz must be a valid IANA timezone "
            f"(e.g. 'America/Los_Angeles', 'UTC'); got {meta['idle_window_tz']!r}"
        )
    if "gha_minutes_cap" in meta:
        cap = meta["gha_minutes_cap"]
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            errors.append("meta.gha_minutes_cap must be a positive integer")
    if "kill_switch" in meta and not isinstance(meta["kill_switch"], bool):
        errors.append("meta.kill_switch must be a bool")

    # 12. neutralized_tasks (optional, but if present must be a list of dicts)
    nts = config.get("neutralized_tasks", [])
    if not isinstance(nts, list):
        errors.append("neutralized_tasks must be a list")
    else:
        for j, nt in enumerate(nts):
            if not isinstance(nt, dict):
                errors.append(f"neutralized_tasks[{j}] must be a mapping")
                continue
            for k in ("task_id", "original_routine_id", "neutralized_at_iter"):
                if k not in nt:
                    errors.append(f"neutralized_tasks[{j}] missing key: {k}")
            # cross-check: a neutralized task_id must not also be in active routines
            tid = nt.get("task_id")
            if tid:
                for r in config.get("routines", []):
                    if r.get("task_id") == tid and r.get("enabled", True):
                        errors.append(
                            f"neutralized_tasks[{j}] task_id {tid!r} is also assigned to active "
                            f"routine {r.get('id')!r} — neutralized tasks must not be reused"
                        )

    # 11. anti-flap (informational, only if history present)
    history_dir = Path(".iteration/history")
    window = meta.get("anti_flap_window", 7)
    if history_dir.is_dir() and window:
        recent_removed: list[str] = []
        try:
            files = sorted(history_dir.glob("iter-*.md"))[-window:]
            for f in files:
                text = f.read_text(errors="ignore")
                # naive parse: lines like "Removed: a, b, c"
                for line in text.splitlines():
                    if line.lower().startswith("- removed:") or line.lower().startswith("removed:"):
                        rest = line.split(":", 1)[1]
                        recent_removed.extend(x.strip() for x in rest.split(",") if x.strip())
            for rid in seen_ids:
                if rid in recent_removed:
                    warnings.append(
                        f"anti-flap: routine {rid!r} was removed within last {window} iters; "
                        f"re-adding may indicate flapping"
                    )
        except Exception as e:
            warnings.append(f"anti-flap check skipped: {e}")

    if warnings:
        # warnings don't fail the check, but are surfaced
        for w in warnings:
            print(f"[warn] {w}", file=sys.stderr)

    return errors


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path", nargs="?", help="path to config.yaml")
    p.add_argument("--stdin", action="store_true", help="read config from stdin")
    p.add_argument("--json", action="store_true", help="emit JSON report")
    args = p.parse_args(argv)

    if args.stdin:
        text = sys.stdin.read()
    elif args.path:
        text = Path(args.path).read_text()
    else:
        print("usage: sanity-check.py <path> | --stdin", file=sys.stderr)
        return 2

    try:
        config = parse_yaml(text)
    except Exception as e:
        msg = f"YAML parse failed: {e}"
        print(json.dumps({"ok": False, "errors": [msg]}) if args.json else msg)
        return 1

    if not isinstance(config, dict):
        msg = "config must be a YAML mapping at top level"
        print(json.dumps({"ok": False, "errors": [msg]}) if args.json else msg)
        return 1

    errors = check(config)
    if errors:
        if args.json:
            print(json.dumps({"ok": False, "errors": errors}, indent=2))
        else:
            print("SANITY CHECK FAILED:")
            for e in errors:
                print(f"  - {e}")
        return 1

    if args.json:
        print(json.dumps({"ok": True, "errors": []}))
    else:
        print("sanity check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
