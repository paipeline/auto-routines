#!/usr/bin/env python3
"""
One-shot renderer used to install auto-routines on its own repo.

Reads .iteration/config.yaml + templates/routine-catalog.yaml + templates/routine-skill.md,
fills in {{placeholders}} for each ACTIVE routine, and writes
.claude/skills/<routine_id>/SKILL.md.

Also installs the shared routine preamble (`templates/routine-preamble.md`)
once per repo at `.claude/skills/_shared/preamble.md` — every per-routine
SKILL references this file via its `## Reference` section, so identical
bytes survive across fires and stay cache-hot.

PRD #10 / slice 1 (token frugality): per-routine SKILL.md is capped by
`meta.max_routine_skill_bytes` (default 3000). Routines with unusually
large prompt bodies may override the cap via `routines[].max_skill_bytes`
in `.iteration/config.yaml`.

This script is *only* used during the self-hosting setup. The skill itself
does the equivalent rendering inline during `init`. Kept in scripts/ for
reproducibility — if you re-run this it overwrites the per-routine SKILLs.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / ".iteration" / "config.yaml").read_text())
CATALOG = yaml.safe_load((ROOT / "templates" / "routine-catalog.yaml").read_text())
TEMPLATE = (ROOT / "templates" / "routine-skill.md").read_text()
PREAMBLE_SRC = ROOT / "templates" / "routine-preamble.md"

ARCHETYPES = {a["id"]: a for a in CATALOG["archetypes"]}

INSTALLED_AT = dt.datetime.now().astimezone().isoformat(timespec="seconds")

# Default cap on per-routine SKILL.md size. PRD #10 / slice 1: the point
# of the shared preamble is to keep each per-routine SKILL small so the
# token footprint per fire stays predictable. A routine that genuinely
# needs more (large prompt_body) can opt out with `max_skill_bytes`.
DEFAULT_MAX_SKILL_BYTES = 3000

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
    "meta-evolve": (
        "- `.iteration/goal.md` (the just-edited PRD — required; this is what changed).\n"
        "- `git show HEAD -- .iteration/goal.md` to see exactly which lines moved.\n"
        "- `.iteration/tasks.md` (the cached task breakdown you'll rewrite — "
        "may not exist on first fire; create it then).\n"
        "- `gh pr list --state open --search 'head:routines/prd-implement'` "
        "(in-flight prd-implement PRs to preserve — do NOT rip out tasks "
        "that already have an open PR)."
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


def _atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via tempfile + os.replace.

    The renderer must be idempotent across re-runs (acceptance criterion
    #3 on issue #94): a torn write on one fire would leave a half-written
    preamble for the next fire to copy from. tempfile + os.replace is
    POSIX-atomic on the same filesystem; we put the tempfile in the
    destination's parent so the rename never crosses devices.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        # If anything went wrong before the replace, clean up.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def install_shared_preamble(skills_dir: Path) -> Path:
    """Copy `templates/routine-preamble.md` to
    `<skills_dir>/_shared/preamble.md`.

    Idempotent: identical input bytes produce an identical output file
    (no timestamps, no per-routine substitution). Every per-routine
    SKILL.md's `## Reference` block points here, so installing this
    once per repo replaces N copies of the same boilerplate."""
    if not PREAMBLE_SRC.exists():
        sys.exit(
            f"missing preamble source: {PREAMBLE_SRC.relative_to(ROOT)} — "
            f"every per-routine SKILL.md references _shared/preamble.md; "
            f"without the source template the install is incomplete"
        )
    dest = skills_dir / "_shared" / "preamble.md"
    _atomic_write(dest, PREAMBLE_SRC.read_text())
    return dest


def resolve_skill_byte_cap(routine: dict, config: dict) -> int:
    """Per-routine override > meta default > hardcoded fallback.

    The override path exists so a routine with a genuinely large
    prompt_body (e.g. `commit-tests` at 4.4KB) can opt out of the cap
    without forcing every other routine to inherit the bigger limit.
    Issue #94 acceptance criterion #5."""
    override = routine.get("max_skill_bytes")
    if isinstance(override, int) and override > 0:
        return override
    meta_cap = (config.get("meta") or {}).get("max_routine_skill_bytes")
    if isinstance(meta_cap, int) and meta_cap > 0:
        return meta_cap
    return DEFAULT_MAX_SKILL_BYTES


def main() -> int:
    skills_dir = ROOT / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    preamble_path = install_shared_preamble(skills_dir)
    print(f"installed {preamble_path.relative_to(ROOT)}")

    oversize: list[tuple[Path, int, int]] = []
    for routine in CONFIG["routines"]:
        rid = routine["id"]
        out_dir = skills_dir / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "SKILL.md"
        rendered = render_one(routine)
        # Sanity: no leftover {{placeholders}}
        if "{{" in rendered or "}}" in rendered:
            sys.exit(f"unfilled placeholder in {out_file}: {rendered[rendered.find('{{'):rendered.find('}}')+2]}")
        _atomic_write(out_file, rendered)
        size = len(rendered.encode("utf-8"))
        cap = resolve_skill_byte_cap(routine, CONFIG)
        marker = "" if size <= cap else f"  [over cap {cap}]"
        print(f"wrote {out_file.relative_to(ROOT)} ({size} bytes){marker}")
        if size > cap:
            oversize.append((out_file, size, cap))

    if oversize:
        print(
            "\nERROR: rendered per-routine SKILL.md exceeded the byte cap "
            "(meta.max_routine_skill_bytes, default 3000). Either trim the "
            "routine's prompt_body, or set `max_skill_bytes: <N>` on the "
            "routine in .iteration/config.yaml.",
            file=sys.stderr,
        )
        for path, size, cap in oversize:
            print(
                f"  - {path.relative_to(ROOT)}: {size} bytes (cap {cap})",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
