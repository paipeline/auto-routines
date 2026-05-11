#!/usr/bin/env python3
"""
status.py — print the auto-routines status block without any LLM tokens.

This is the implementation of `/auto-routines status`. It reads
`.iteration/config.yaml` and `.iteration/log.jsonl` from the current working
directory and prints the same status block format documented in SKILL.md.

The skill's `status` mode is just `python3 scripts/status.py` — no Claude
analysis, no file inspection by an agent, instant output.

Live-monitor flags (issue #82):
  --watch [N]      refresh the render every N seconds (default 5). Uses
                   ANSI escape codes to clear the screen — never spawns
                   `os.system("clear")` (the locality contract forbids it).
                   Ctrl-C exits cleanly.
  --since <dur>    filter the activity tail to fires within the given
                   duration. Accepts `30s`, `15m`, `2h`, `7d`. Bare ints
                   and unknown units are rejected with rc=2.
  --routine <id>   show only the named routine — current FSM state, last
                   20 fires with outcomes + summary + PR URL when present.
                   Unknown id error lists valid ids.

Exit codes:
  0  = printed status (or `.iteration/halted.md` notice)
  1  = no `.iteration/config.yaml` found / unknown --routine id
  2  = config exists but is unreadable / malformed --since duration
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("status: PyYAML not installed (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


STATE_ORDER = ["ACTIVE", "EVOLVING", "STAGNANT", "COMPLETED", "STOPPED", "PROPOSED"]

# ANSI escape: clear screen (\033[2J) and move cursor to home (\033[H).
# Pure stdout — no subprocess, no os.system. Required by the locality
# contract pinned in tests/test_status.py::test_status_does_not_invoke_claude.
ANSI_CLEAR = "\033[2J\033[H"

# Sentinel used by argparse so `--watch` with no value defaults to 5 seconds
# while still distinguishing "not passed" from "passed without value".
_WATCH_DEFAULT_INTERVAL = 5


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


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86_400}


def parse_duration(s: str) -> int:
    """Parse a duration string like `30s`, `15m`, `2h`, `7d` to seconds.

    Rejects bare ints (ambiguous unit), unknown units (`5w`), the empty
    string, and anything that doesn't match `<int><smhd>`.

    Raises ValueError with a human-readable message on rejection — the
    CLI catches that and exits rc=2 with the message.
    """
    if not s:
        raise ValueError("empty duration")
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise ValueError(
            f"unrecognised duration {s!r}; expected <int><unit> "
            f"where unit is one of s/m/h/d (e.g. 30m, 2h, 7d)"
        )
    n = int(m.group(1))
    unit = m.group(2)
    return n * _DURATION_UNIT_SECONDS[unit]


def filter_log_since(log: list[dict], since_seconds: int) -> list[dict]:
    """Return entries whose `ts` parses and falls within the last
    `since_seconds`. Entries with unparseable `ts` are dropped (we
    can't tell whether they fall in the window)."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    out: list[dict] = []
    for entry in log:
        dt = parse_iso(entry.get("ts", ""))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            out.append(entry)
    return out


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


def _render_routine_drill_in(
    config: dict,
    root: Path,
    routine_id: str,
    since_seconds: int | None,
    as_json: bool,
) -> tuple[int, str]:
    """Render the --routine drill-in view. Returns `(rc, output)`.

    Issue #82 upgrades:
    - last 20 fires (was 10);
    - includes `pr_url` from log entries when present;
    - unknown id error lists all valid ids;
    - composes with `--since` to filter the recent tail.
    """
    routines = [r for r in config.get("routines", []) if r.get("id") == routine_id]
    if not routines:
        valid_ids = sorted(r.get("id", "") for r in config.get("routines", []))
        ids_listing = ", ".join(valid_ids) if valid_ids else "(none)"
        msg = (
            f"status: no routine with id={routine_id!r}. "
            f"Valid ids: {ids_listing}\n"
        )
        return 1, msg

    log = tail_jsonl(root / ".iteration" / "log.jsonl", n=500)
    r = routines[0]
    runs, useful, noisy, last_outcome, last_ts = stats_for(r, log)
    matches = [e for e in log if e.get("routine") == routine_id]
    if since_seconds is not None:
        matches = filter_log_since(matches, since_seconds)
    recent = matches[-20:]

    if as_json:
        return 0, json.dumps({
            "routine": r,
            "runs": runs,
            "useful": useful,
            "noisy": noisy,
            "last_outcome": last_outcome,
            "last_ts": last_ts,
            "recent": recent,
        }, indent=2) + "\n"

    out_lines = [
        f"routine:  {r.get('id')}",
        f"state:    {r.get('state')}",
        f"trigger:  {(r.get('trigger') or {}).get('human')}",
        f"purpose:  {r.get('purpose')}",
        f"runs:     {runs}  useful: {useful}  noisy: {noisy}",
        f"last:     {last_outcome or '—'} {relative(last_ts)}",
    ]
    if recent:
        out_lines.append("recent log:")
        for e in recent:
            line = (
                f"  - {e.get('ts')}  {e.get('outcome')}  "
                f"{e.get('summary', '')}"
            )
            pr_url = e.get("pr_url")
            if pr_url:
                line += f"  [PR: {pr_url}]"
            out_lines.append(line)
    return 0, "\n".join(out_lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser. Exposed for tests + the SKILL.md
    drift detector that asserts doc <-> parser parity."""
    p = argparse.ArgumentParser(description="Print auto-routines status (no LLM).")
    p.add_argument(
        "--routine",
        help="show only one routine in detail (last 20 fires, PR URL when present)",
    )
    p.add_argument("--root", default=".", help="repo root (default: cwd)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument(
        "--watch",
        nargs="?",
        const=_WATCH_DEFAULT_INTERVAL,
        default=None,
        type=int,
        metavar="N",
        help=(
            "refresh the render every N seconds (default 5). Uses "
            "ANSI clear-screen; Ctrl-C exits cleanly."
        ),
    )
    p.add_argument(
        "--since",
        default=None,
        metavar="DURATION",
        help=(
            "filter the activity tail to fires within DURATION "
            "(e.g. 30s, 15m, 2h, 7d)"
        ),
    )
    return p


def _one_shot(args: argparse.Namespace, root: Path, config: dict) -> tuple[int, str, str]:
    """Run a single render pass. Returns `(rc, stdout, stderr)`."""
    # Apply --since up front for the table render so the activity tail
    # respects the window.
    if args.routine:
        rc, out = _render_routine_drill_in(
            config, root, args.routine, _since_seconds(args), args.json
        )
        return rc, out if rc == 0 else "", "" if rc == 0 else out

    if args.json:
        return 0, json.dumps({
            "goal": config.get("goal"),
            "mode": config.get("mode"),
            "budget": (config.get("meta") or {}).get("budget", "medium"),
            "routines": config.get("routines", []),
        }, indent=2) + "\n", ""

    return 0, render(config, root), ""


def _since_seconds(args: argparse.Namespace) -> int | None:
    if args.since is None:
        return None
    return parse_duration(args.since)


def main(argv: list[str]) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    config = load_config(root)

    # Validate --since up front so a malformed value fails fast even
    # in --watch mode (otherwise the user sees the error on every
    # tick).
    try:
        _since_seconds(args)
    except ValueError as e:
        print(f"status: --since: {e}", file=sys.stderr)
        return 2

    if args.watch is None:
        rc, out, err = _one_shot(args, root, config)
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        return rc

    # Watch mode: clear, render, sleep, repeat. Ctrl-C exits rc=0.
    interval = max(1, int(args.watch))
    try:
        while True:
            # Re-load config each tick so changes (e.g. a routine
            # paused via `/auto-routines stop`) reflect live.
            config = load_config(root)
            rc, out, err = _one_shot(args, root, config)
            sys.stdout.write(ANSI_CLEAR)
            if out:
                sys.stdout.write(out)
            if err:
                sys.stderr.write(err)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C — print a newline so the next shell
        # prompt is on its own line.
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
