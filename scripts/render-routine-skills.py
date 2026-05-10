#!/usr/bin/env python3
"""
Renderer for auto-routines per-routine SKILL.md files (PRD #10 Module 3).

Reads .iteration/config.yaml + templates/routine-catalog.yaml +
templates/routine-skill.md (slim) + templates/routine-preamble.md (shared),
fills in {{placeholders}} for each ACTIVE routine, and writes:

  .claude/skills/<routine_id>/SKILL.md   (per-routine, ≤3KB)
  .claude/skills/_shared/preamble.md     (shared, install-once)

Public surface (importable as a module — see tests/test_render.py):

  render_routine_skill(template_text, routine, archetype, installed_at) -> str
  render_preamble(preamble_text) -> str
  ROUTINE_SPECIFIC_INPUTS: dict[str, str]   (routine-id → extra inputs block)

Side-effect entry point:

  main()  — reads config from disk, writes rendered files. Used by
            SKILL.md install step 6c-6d.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Routine-specific inputs (the per-archetype 'extra Inputs' bullets).
# Keep one source of truth — these used to live in a giant inline string in
# the template; lifting them here lets the template be archetype-agnostic.
# ---------------------------------------------------------------------------

ROUTINE_SPECIFIC_INPUTS: dict[str, str] = {
    "prd-implement": (
        "- `.iteration/goal.md` (the canonical PRD — required).\n"
        "- `.iteration/tasks.md` (cached task breakdown, if present).\n"
        "- `gh pr list --state all --search 'head:routines/prd-implement' --limit 20` "
        "(your own past PRs, to avoid double-implementing)."
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


# ---------------------------------------------------------------------------
# Pure rendering — no I/O. Tested in tests/test_render.py.
# ---------------------------------------------------------------------------

def render_routine_skill(
    template_text: str,
    routine: dict,
    archetype: dict,
    installed_at: str,
) -> str:
    """Render one per-routine SKILL.md from the slim template.

    Pure function: takes its inputs explicitly, returns the rendered string.
    Does not touch the filesystem. The byte-budget rule (≤3KB) is enforced
    elsewhere — this function does no budget checking itself.

    Args:
        template_text: contents of templates/routine-skill.md
        routine: a routine entry from .iteration/config.yaml's routines list
        archetype: the matching archetype from templates/routine-catalog.yaml
        installed_at: ISO8601 local-time string (with tz offset, never `Z`)

    Returns:
        The rendered SKILL.md as a string. No `{{placeholders}}` should
        remain — caller may assert this.
    """
    rid = routine["id"]
    body = archetype["prompt_body"]
    trigger_summary = routine["trigger"]["human"]
    success = routine.get("success_criterion") or "(none — runs indefinitely)"
    extra_inputs = ROUTINE_SPECIFIC_INPUTS.get(rid, "")

    text = template_text
    text = text.replace("{{routine_id}}", rid)
    text = text.replace("{{purpose}}", routine["purpose"])
    text = text.replace("{{installed_at}}", installed_at)
    text = text.replace("{{iter_added}}", str(routine["iter_added"]))
    text = text.replace("{{primitive}}", routine["primitive"])
    text = text.replace("{{trigger_summary}}", trigger_summary)
    text = text.replace("{{success_criterion}}", success)
    text = text.replace("{{routine_specific_inputs}}", extra_inputs)
    text = text.replace("{{routine_prompt_body}}", body)

    # Collapse the empty-extra-inputs case: if a routine has no extra inputs,
    # the template's `{{routine_specific_inputs}}` line becomes a blank line
    # between bullets. Tidy it up.
    text = text.replace("\n\n## What to do", "\n## What to do")

    return text


def render_preamble(preamble_text: str) -> str:
    """Render the shared preamble. No substitution — preamble is literal
    content shared across every install. Returned as-is, but routed
    through this function so future tweaks (e.g. injecting an
    `installed_at` comment) have a single seam."""
    return preamble_text


# ---------------------------------------------------------------------------
# Side-effect entry point — used by SKILL.md install step.
# ---------------------------------------------------------------------------

def _now_local_iso() -> str:
    """Match the local-time rule: never UTC `Z`."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


DEFAULT_MAX_ROUTINE_SKILL_BYTES = 3000


def _import_sanity():
    """Load scripts/sanity-check.py (the one beside this file) despite the
    hyphen in its filename. Always uses the renderer's own scripts/ dir,
    not the target repo — those should be the same in production but tests
    pass a fixture repo as repo_root."""
    import importlib.util
    sibling = Path(__file__).resolve().parent / "sanity-check.py"
    spec = importlib.util.spec_from_file_location("_sanity_for_renderer", sibling)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main(repo_root: Optional[Path] = None) -> int:
    root = repo_root or ROOT

    config_path = root / ".iteration" / "config.yaml"
    catalog_path = root / "templates" / "routine-catalog.yaml"
    template_path = root / "templates" / "routine-skill.md"
    preamble_path = root / "templates" / "routine-preamble.md"

    if not config_path.exists():
        sys.exit(f"missing {config_path} — run `/auto-routines init` first")
    if not preamble_path.exists():
        sys.exit(
            f"missing {preamble_path} — install is incomplete, "
            f"PRD #10 Module 3 requires this file"
        )

    config = yaml.safe_load(config_path.read_text())
    catalog = yaml.safe_load(catalog_path.read_text())
    template_text = template_path.read_text()
    preamble_text = preamble_path.read_text()

    archetypes = {a["id"]: a for a in catalog["archetypes"]}
    sanity = _import_sanity()
    default_limit = (config.get("meta") or {}).get(
        "max_routine_skill_bytes", DEFAULT_MAX_ROUTINE_SKILL_BYTES
    )

    skills_dir = root / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # 1. Render the shared preamble once.
    shared_dir = skills_dir / "_shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    rendered_preamble = render_preamble(preamble_text)
    if "{{" in rendered_preamble or "}}" in rendered_preamble:
        sys.exit("preamble contains unfilled placeholders")
    (shared_dir / "preamble.md").write_text(rendered_preamble)
    print(f"wrote .claude/skills/_shared/preamble.md")

    # 2. Render each per-routine SKILL.md.
    installed_at = _now_local_iso()
    for routine in config["routines"]:
        rid = routine["id"]
        arch = archetypes.get(rid)
        if not arch:
            sys.exit(f"no archetype matches routine id={rid!r}")

        rendered = render_routine_skill(
            template_text=template_text,
            routine=routine,
            archetype=arch,
            installed_at=installed_at,
        )
        if "{{" in rendered or "}}" in rendered:
            sys.exit(f"unfilled placeholder in rendered SKILL for {rid!r}")

        # Byte-budget enforcement: per-routine override > meta default > 3000.
        limit = routine.get("max_skill_bytes", default_limit)
        budget_errors = sanity.check_rendered_skill_size(rendered, limit, rid)
        if budget_errors:
            for e in budget_errors:
                print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        out_dir = skills_dir / rid
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "SKILL.md"
        out_file.write_text(rendered)
        print(f"wrote {out_file.relative_to(root)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
