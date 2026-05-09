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
import os
import re
import sys
from pathlib import Path

try:
    import yaml  # PyYAML — most dev machines have it
except ImportError:
    yaml = None


KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
PRIMITIVES = {"hook", "scheduled", "loop", "pr-poll", "git-hook"}
LEVELS = {"off", "notify", "suggest", "auto"}
MODES = {"goal-driven", "fully-auto"}
GH_VALUES = {"required", "optional", "none"}
HOOK_EVENTS = {
    "PreToolUse", "PostToolUse", "Notification", "Stop", "SubagentStop",
    "UserPromptSubmit", "PreCompact", "SessionStart", "SessionEnd",
}
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
        # 6. trigger fields per primitive
        trig = r.get("trigger", {}) or {}
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
