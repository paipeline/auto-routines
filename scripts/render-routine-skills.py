#!/usr/bin/env python3
"""
One-shot renderer used to install auto-routines on its own repo.

Reads .iteration/config.yaml + templates/routine-catalog.yaml + templates/routine-skill.md,
fills in {{placeholders}} for each ACTIVE routine, and writes
.claude/skills/<routine_id>/SKILL.md.

This script is *only* used during the self-hosting setup. The skill itself
does the equivalent rendering inline during `init`. Kept in scripts/ for
reproducibility — if you re-run this it overwrites the per-routine SKILLs.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / ".iteration" / "config.yaml").read_text())
CATALOG = yaml.safe_load((ROOT / "templates" / "routine-catalog.yaml").read_text())
TEMPLATE = (ROOT / "templates" / "routine-skill.md").read_text()

ARCHETYPES = {a["id"]: a for a in CATALOG["archetypes"]}

INSTALLED_AT = dt.datetime.now().astimezone().isoformat(timespec="seconds")

# Per-routine context that doesn't fit cleanly in the catalog body.
ROUTINE_SPECIFIC_INPUTS = {
    "prd-implement": (
        "- `.iteration/goal.md` (the canonical PRD — required).\n"
        "- `.iteration/tasks.md` (cached task breakdown, if present).\n"
        "- `gh pr list --state all --search 'head:routines/prd-implement' --limit 20` "
        "(your own past PRs, to avoid double-implementing).\n"
        "- For self-hosted (this repo): `/tmp/auto-routines-test/iter-NNN-<slice>/` is "
        "a temp repo you may create to validate a change end-to-end before opening "
        "the PR. Tear it down on success; preserve on failure and reference the path "
        "in the PR body."
    ),
    "commit-tests": (
        "- `git show HEAD --stat` and `git show HEAD -- <changed files>` "
        "for the just-committed change.\n"
        "- The pytest output (run `pytest -q` with a 5-minute timeout)."
    ),
    "commit-lint": (
        "- `git diff HEAD~1 HEAD` for the changed files.\n"
        "- Available linters detected from `pyproject.toml` (ruff, mypy) and "
        "`package.json` (eslint, prettier) if present."
    ),
    "session-doc-drift": (
        "- `README.md`, `SKILL.md`, `templates/routine-catalog.yaml`, "
        "`templates/routine-skill.md` (the docs that must stay in sync).\n"
        "- `git diff` of the session against these files to spot which doc "
        "has fallen behind code."
    ),
    "daily-digest": (
        '- `git log --since="00:00 today" --pretty=format:"%h %s (%an)"`\n'
        "- `gh pr list --state all --search \"updated:>$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)\"`\n"
        "- Tail of `.iteration/log.jsonl` since 00:00 today."
    ),
}

SELF_EVOLVE_ON = """\
You may file a mid-run evolve request if you decide your own config is wrong
(too frequent, too rare, scope drift, no longer useful). Append one JSON line
to `.iteration/evolve_requests.jsonl`:

```json
{"ts":"<local ISO8601 with offset>","routine_id":"<your id>","reason":"<one sentence>","suggested":"<one sentence>"}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z`. The always-on `Stop` hook
fires `/auto-routines evolve` at the end of the next Claude session, which
drains the file.
"""

SELF_EVOLVE_OFF = (
    "(self-evolve not enabled for this routine — your config is fixed by the "
    "user. Do not write to `evolve_requests.jsonl`.)"
)


def render_one(routine: dict) -> str:
    rid = routine["id"]
    arch = ARCHETYPES.get(rid)
    if not arch:
        sys.exit(f"no archetype matches routine id={rid!r}")
    body = arch["prompt_body"]

    text = TEMPLATE
    text = text.replace("{{routine_id}}", rid)
    text = text.replace("{{purpose}}", routine["purpose"])
    text = text.replace("{{installed_at}}", INSTALLED_AT)
    text = text.replace("{{iter_added}}", str(routine["iter_added"]))
    text = text.replace("{{primitive}}", routine["primitive"])
    text = text.replace("{{trigger_summary}}", routine["trigger"]["human"])
    text = text.replace(
        "{{success_criterion}}",
        routine.get("success_criterion") or "(none — runs indefinitely)",
    )
    text = text.replace(
        "{{routine_specific_inputs}}",
        ROUTINE_SPECIFIC_INPUTS.get(rid, "(no extra inputs)"),
    )
    text = text.replace("{{routine_prompt_body}}", body)
    text = text.replace(
        "{{self_evolve_block}}",
        SELF_EVOLVE_ON if routine.get("self_evolve") else SELF_EVOLVE_OFF,
    )
    return text


def main() -> int:
    skills_dir = ROOT / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    for routine in CONFIG["routines"]:
        rid = routine["id"]
        out_dir = skills_dir / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "SKILL.md"
        rendered = render_one(routine)
        # Sanity: no leftover {{placeholders}}
        if "{{" in rendered or "}}" in rendered:
            sys.exit(f"unfilled placeholder in {out_file}: {rendered[rendered.find('{{'):rendered.find('}}')+2]}")
        out_file.write_text(rendered)
        print(f"wrote {out_file.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
