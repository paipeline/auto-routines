#!/usr/bin/env python3
"""
coordinator-brief.py — generate the structured brief the central coordinator
agent reads at the start of every scheduled fire.

This is the *information* layer of the dispatcher pattern. It runs as pure
shell (no LLM, no network beyond `gh`/`git`) and emits a single Markdown
document the coordinator inlines into its decision step. The coordinator
itself is a Claude session; the brief is what makes that session cheap and
deterministic — instead of letting the LLM grovel through `git log`, log.jsonl,
and PR lists, we hand it the answers.

Structure of the brief:
  - Header (now, last fire of coordinator, budget tier, mode)
  - PRD progress (counts of [x] vs [ ] in goal.md)
  - Routine roster (state, last fire, last outcome, runs/useful/noisy)
  - Open routine PRs (gh pr list filtered to routines/* branches)
  - Recent commits (since last coordinator fire)
  - Pending evolve requests
  - Last N coordinator decisions (for stagnation detection — were we deciding
    the same thing every fire?)

Exit codes:
  0  = brief printed
  1  = no .iteration/config.yaml in this repo
  2  = config unreadable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("coordinator-brief: PyYAML not installed (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 15) -> tuple[int, str]:
    """Best-effort shell call. Returns (rc, stdout). Empty stdout on any error."""
    try:
        out = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GH_PAGER": "", "PAGER": ""},
        )
        return out.returncode, out.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 1, ""


def load_config(root: Path) -> dict:
    cfg_path = root / ".iteration" / "config.yaml"
    if not cfg_path.is_file():
        print(
            "coordinator-brief: no .iteration/config.yaml in this repo. "
            "Run `/auto-routines` to install.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return yaml.safe_load(cfg_path.read_text()) or {}
    except Exception as e:
        print(f"coordinator-brief: cannot parse config.yaml: {e}", file=sys.stderr)
        sys.exit(2)


def tail_jsonl(path: Path, n: int = 200) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for raw in path.read_text().splitlines()[-n:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def count_prd_progress(goal_path: Path) -> tuple[int, int, list[str]]:
    """Return (done, todo, first_3_unchecked_titles) by counting checkboxes."""
    if not goal_path.is_file():
        return 0, 0, []
    text = goal_path.read_text()
    done = len(re.findall(r"^\s*-\s*\[x\]", text, flags=re.MULTILINE | re.IGNORECASE))
    todo_lines = re.findall(r"^\s*-\s*\[ \]\s+(.+)$", text, flags=re.MULTILINE)
    return done, len(todo_lines), [t.strip() for t in todo_lines[:3]]


def last_fire_of(log: list[dict], routine: str) -> dict | None:
    for entry in reversed(log):
        if entry.get("routine") == routine:
            return entry
    return None


def relative(ts: str | None) -> str:
    if not ts:
        return "never"
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if re.match(r".*[+-]\d{4}$", s):
        s = s[:-5] + s[-5:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(tz=dt.tzinfo) - dt).total_seconds())
    if secs < 0:
        return f"{abs(secs)}s from now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86_400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86_400}d ago"


def open_routine_prs(root: Path) -> list[dict]:
    """List open PRs whose branch starts with `routines/`. `gh` is best-effort."""
    if shutil.which("gh") is None:
        return []
    rc, out = _run(
        ["gh", "pr", "list", "--state", "open",
         "--search", "head:routines/", "--limit", "30",
         "--json", "number,title,headRefName,updatedAt,isDraft"],
        cwd=root,
    )
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def recent_commits_since(root: Path, since_iso: str | None, limit: int = 30) -> list[str]:
    """Last `limit` commits, optionally filtered to >=since_iso. Returns sha+subj lines."""
    if shutil.which("git") is None:
        return []
    args = ["git", "log", "--pretty=format:%h %ad %s", "--date=iso-strict", f"-{limit}"]
    if since_iso:
        args.insert(2, f"--since={since_iso}")
    rc, out = _run(args, cwd=root)
    if rc != 0:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def pending_evolve_count(root: Path) -> int:
    p = root / ".iteration" / "evolve_requests.jsonl"
    if not p.is_file():
        return 0
    return sum(1 for ln in p.read_text().splitlines() if ln.strip())


def coordinator_decision_history(log: list[dict], n: int = 5) -> list[dict]:
    """Return the last `n` coordinator entries (most recent last)."""
    return [e for e in log if e.get("routine") == "coordinator"][-n:]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render(config: dict, root: Path) -> str:
    log = tail_jsonl(root / ".iteration" / "log.jsonl", n=500)
    coord_history = coordinator_decision_history(log)
    last_coord = coord_history[-1] if coord_history else None
    since_iso = last_coord.get("ts") if last_coord else None

    done, todo, next_three = count_prd_progress(root / ".iteration" / "goal.md")
    prs = open_routine_prs(root)
    commits = recent_commits_since(root, since_iso=since_iso, limit=30)
    pending = pending_evolve_count(root)

    meta = config.get("meta") or {}
    budget = meta.get("budget", "medium")
    mode = config.get("mode", "?")
    repo_slug = config.get("repo_slug", "?")
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    lines: list[str] = []
    lines.append("# coordinator brief")
    lines.append("")
    lines.append(f"- now: {now_iso}")
    lines.append(f"- repo: {repo_slug}")
    lines.append(f"- mode: {mode}")
    lines.append(f"- budget: {budget}")
    lines.append(f"- last coordinator fire: {relative(since_iso)}")
    lines.append("")

    lines.append("## PRD progress")
    total = done + todo
    pct = (100 * done // total) if total else 0
    lines.append(f"- done: {done}/{total} ({pct}%)")
    if next_three:
        lines.append("- next unchecked slices:")
        for t in next_three:
            lines.append(f"  - {t}")
    else:
        lines.append("- (no unchecked slices — PRD may be complete)")
    lines.append("")

    lines.append("## Routine roster")
    lines.append("| id | state | last fire | last outcome | runs | useful | noisy |")
    lines.append("|----|-------|-----------|--------------|------|--------|-------|")
    for r in config.get("routines", []):
        rid = r.get("id", "?")
        state = r.get("state", "ACTIVE")
        last = last_fire_of(log, rid)
        last_ts = last.get("ts") if last else None
        last_outcome = last.get("outcome") if last else "—"
        runs = sum(1 for e in log if e.get("routine") == rid)
        useful = sum(1 for e in log if e.get("routine") == rid and e.get("increment_signal"))
        noisy = sum(1 for e in log if e.get("routine") == rid and e.get("outcome") in {"warn", "err"})
        lines.append(
            f"| {rid} | {state} | {relative(last_ts)} | {last_outcome} | "
            f"{runs} | {useful} | {noisy} |"
        )
    lines.append("")

    lines.append("## Open routine PRs")
    if not prs:
        lines.append("- (none)")
    else:
        for pr in prs:
            draft = " [DRAFT]" if pr.get("isDraft") else ""
            lines.append(
                f"- #{pr.get('number')} `{pr.get('headRefName')}` — {pr.get('title')}{draft} "
                f"(updated {relative(pr.get('updatedAt'))})"
            )
    lines.append("")

    lines.append("## Recent commits (since last coordinator fire)")
    if not commits:
        lines.append("- (none)")
    else:
        for c in commits[:15]:
            lines.append(f"- {c}")
        if len(commits) > 15:
            lines.append(f"- ... and {len(commits) - 15} more")
    lines.append("")

    lines.append(f"## Pending evolve requests: {pending}")
    lines.append("")

    lines.append("## Last 5 coordinator decisions")
    if not coord_history:
        lines.append("- (none yet)")
    else:
        for e in coord_history:
            ts = e.get("ts", "?")
            outcome = e.get("outcome", "?")
            summary = e.get("summary", "")
            lines.append(f"- {ts} [{outcome}] {summary}")

    return "\n".join(lines) + "\n"


def render_json(config: dict, root: Path) -> dict[str, Any]:
    log = tail_jsonl(root / ".iteration" / "log.jsonl", n=500)
    coord_history = coordinator_decision_history(log)
    last_coord = coord_history[-1] if coord_history else None
    since_iso = last_coord.get("ts") if last_coord else None
    done, todo, next_three = count_prd_progress(root / ".iteration" / "goal.md")
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repo_slug": config.get("repo_slug"),
        "mode": config.get("mode"),
        "budget": (config.get("meta") or {}).get("budget", "medium"),
        "last_coordinator_fire": since_iso,
        "prd": {"done": done, "todo": todo, "next_three": next_three},
        "routines": [
            {
                "id": r.get("id"),
                "state": r.get("state", "ACTIVE"),
                "last_fire": (last_fire_of(log, r.get("id", "")) or {}).get("ts"),
                "last_outcome": (last_fire_of(log, r.get("id", "")) or {}).get("outcome"),
                "runs": sum(1 for e in log if e.get("routine") == r.get("id")),
                "useful": sum(
                    1 for e in log if e.get("routine") == r.get("id") and e.get("increment_signal")
                ),
                "noisy": sum(
                    1 for e in log if e.get("routine") == r.get("id")
                    and e.get("outcome") in {"warn", "err"}
                ),
            }
            for r in config.get("routines", [])
        ],
        "open_prs": open_routine_prs(root),
        "recent_commits": recent_commits_since(root, since_iso=since_iso, limit=30),
        "pending_evolve_requests": pending_evolve_count(root),
        "coordinator_history": coord_history,
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Print the coordinator brief (no LLM).")
    p.add_argument("--root", default=".", help="repo root (default: cwd)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    config = load_config(root)

    if args.json:
        print(json.dumps(render_json(config, root), indent=2, default=str))
    else:
        sys.stdout.write(render(config, root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
