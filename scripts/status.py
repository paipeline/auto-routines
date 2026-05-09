#!/usr/bin/env python3
"""
status.py — print the auto-routines status block without any LLM tokens.

This is the implementation of `/auto-routines status`. It reads
`.iteration/config.yaml` and `.iteration/log.jsonl` from the current working
directory and prints the same status block format documented in SKILL.md.

The skill's `status` mode is just `python3 scripts/status.py` — no Claude
analysis, no file inspection by an agent, instant output.

Exit codes:
  0  = printed status (or `.iteration/halted.md` notice)
  1  = no `.iteration/config.yaml` found (this repo isn't a consumer)
  2  = config exists but is unreadable
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("status: PyYAML not installed (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


STATE_ORDER = ["ACTIVE", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED", "PROPOSED"]


def load_config(root: Path) -> dict:
    cfg_path = root / ".iteration" / "config.yaml"
    if not cfg_path.is_file():
        print(
            "status: no .iteration/config.yaml in this repo. "
            "Run `/auto-routines` to install.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return yaml.safe_load(cfg_path.read_text()) or {}
    except Exception as e:
        print(f"status: cannot parse config.yaml: {e}", file=sys.stderr)
        sys.exit(2)


def tail_jsonl(path: Path, n: int = 200) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        for raw in path.read_text().splitlines()[-n:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out


def parse_iso(ts: str) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.match(r".*[+-]\d{4}$", s):
        s = s[:-5] + s[-5:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def relative(ts: str | None) -> str:
    dt = parse_iso(ts) if ts else None
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(tz=dt.tzinfo) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = abs(secs)
        suffix = "from now"
    else:
        suffix = "ago"
    if secs < 60:
        return f"{secs}s {suffix}"
    if secs < 3600:
        return f"{secs // 60}m {suffix}"
    if secs < 86_400:
        return f"{secs // 3600}h {suffix}"
    return f"{secs // 86_400}d {suffix}"


def stats_for(routine: dict, log: list[dict]) -> tuple[int, int, int, str | None, str | None]:
    rid = routine["id"]
    base = routine.get("stats") or {}
    runs = int(base.get("runs", 0))
    useful = int(base.get("useful", 0))
    noisy = int(base.get("noisy", 0))
    last_outcome: str | None = None
    last_ts: str | None = None
    matches = [e for e in log if e.get("routine") == rid]
    runs = max(runs, len(matches))
    useful = max(useful, sum(1 for e in matches if e.get("increment_signal")))
    noisy = max(noisy, sum(1 for e in matches if e.get("outcome") in {"warn", "err"}))
    if matches:
        last = matches[-1]
        last_outcome = last.get("outcome")
        last_ts = last.get("ts")
    return runs, useful, noisy, last_outcome, last_ts


def render(config: dict, root: Path) -> str:
    log = tail_jsonl(root / ".iteration" / "log.jsonl", n=500)
    pending_evolve = (
        sum(1 for _ in (root / ".iteration" / "evolve_requests.jsonl").read_text().splitlines())
        if (root / ".iteration" / "evolve_requests.jsonl").is_file()
        else 0
    )

    routines = list(config.get("routines", []))
    routines.sort(
        key=lambda r: (
            STATE_ORDER.index(r.get("state", "ACTIVE"))
            if r.get("state", "ACTIVE") in STATE_ORDER
            else len(STATE_ORDER),
            r.get("id", ""),
        )
    )

    # Header
    goal = (config.get("goal") or "(no goal set)").strip().splitlines()[0]
    if len(goal) > 60:
        goal = goal[:57] + "..."
    mode = config.get("mode", "?")
    budget = (config.get("meta") or {}).get("budget", "medium")
    meta = config.get("meta") or {}
    meta_human = meta.get("human", meta.get("cron", "?"))
    meta_last_run = meta.get("last_run")

    lines = [
        f"goal:        {goal}     mode: {mode}     budget: {budget}",
        f"meta evolve: {meta_human}   ─   last fired {relative(meta_last_run)}",
        "",
    ]

    # Table
    headers = ("routine", "schedule", "state", "runs", "useful", "noisy", "last")
    widths = [18, 26, 10, 5, 7, 6, 24]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines.append(fmt.format(*headers))
    lines.append("  ".join("─" * w for w in widths))

    if not routines:
        lines.append("(no routines installed)")
    for r in routines:
        rid = r.get("id", "?")
        sched = (r.get("trigger") or {}).get("human") or (r.get("trigger") or {}).get("cron") or "?"
        if len(sched) > widths[1]:
            sched = sched[: widths[1] - 1] + "…"
        state = r.get("state", "ACTIVE")
        runs, useful, noisy, last_outcome, last_ts = stats_for(r, log)
        last = f"{last_outcome or '—'} {relative(last_ts)}" if last_ts else "never"
        if len(last) > widths[6]:
            last = last[: widths[6] - 1] + "…"
        lines.append(fmt.format(rid, sched, state, runs, useful, noisy, last))

    lines.append("")
    lines.append(f"evolve requests pending: {pending_evolve}")
    last_iter = "—"
    history = sorted((root / ".iteration" / "history").glob("iter-*.md")) \
        if (root / ".iteration" / "history").is_dir() else []
    if history:
        last = history[-1]
        last_iter = f"{last.stem} — modified {relative(datetime.fromtimestamp(last.stat().st_mtime, tz=timezone.utc).isoformat())}"
    lines.append(f"last iter:               {last_iter}")

    halted = root / ".iteration" / "halted.md"
    if halted.is_file():
        lines.append("")
        lines.append("⚠ halted: see .iteration/halted.md")

    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Print auto-routines status (no LLM).")
    p.add_argument("--routine", help="show only one routine in detail")
    p.add_argument("--root", default=".", help="repo root (default: cwd)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    config = load_config(root)

    if args.routine:
        routines = [r for r in config.get("routines", []) if r.get("id") == args.routine]
        if not routines:
            print(f"status: no routine with id={args.routine!r}", file=sys.stderr)
            return 1
        log = tail_jsonl(root / ".iteration" / "log.jsonl", n=500)
        r = routines[0]
        runs, useful, noisy, last_outcome, last_ts = stats_for(r, log)
        recent = [e for e in log if e.get("routine") == args.routine][-10:]
        if args.json:
            print(json.dumps({
                "routine": r,
                "runs": runs,
                "useful": useful,
                "noisy": noisy,
                "last_outcome": last_outcome,
                "last_ts": last_ts,
                "recent": recent,
            }, indent=2))
            return 0
        print(f"routine:  {r.get('id')}")
        print(f"state:    {r.get('state')}")
        print(f"trigger:  {(r.get('trigger') or {}).get('human')}")
        print(f"purpose:  {r.get('purpose')}")
        print(f"runs:     {runs}  useful: {useful}  noisy: {noisy}")
        print(f"last:     {last_outcome or '—'} {relative(last_ts)}")
        if recent:
            print("recent log:")
            for e in recent:
                print(f"  - {e.get('ts')}  {e.get('outcome')}  {e.get('summary', '')}")
        return 0

    if args.json:
        print(json.dumps({
            "goal": config.get("goal"),
            "mode": config.get("mode"),
            "budget": (config.get("meta") or {}).get("budget", "medium"),
            "routines": config.get("routines", []),
        }, indent=2))
        return 0

    sys.stdout.write(render(config, root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
