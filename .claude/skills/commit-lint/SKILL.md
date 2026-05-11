---
name: commit-lint
description: Run linters after every commit; auto-fix and PR when violations exist. — installed by auto-routines on 2026-05-11T13:57:34+02:00, iter-1. Invoked by git-hook trigger (on every git commit).
---

# commit-lint

## Purpose
Run linters after every commit; auto-fix and PR when violations exist.

## Trigger
on every git commit

## Success criterion
0 lint violations in last 20 commits

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- `git diff HEAD~1 HEAD` for the changed files.
- Available linters detected from `pyproject.toml` (ruff, mypy) and `package.json` (eslint, prettier) if present.

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

The user just committed. Your job:

1. Detect available linters/formatters (ruff, eslint, mypy, prettier, gofmt,
   clippy, rubocop). Skip any not present.
2. Run each in --fix mode (or formatter equivalent). If anything changed:
   a. Verify the changes are pure formatting/style — no semantic edits.
      If the linter wants a behavior change (e.g. ruff's `B008`), DO NOT
      apply it; only flag.
   b. Branch: `routines/commit-lint`. Commit with message
      `style: auto-fix lint violations from <short SHA>`.
   c. Open PR via `python3 scripts/orchestrator.py open-pr
      --head routines/commit-lint --title 'style: auto-fix lint
      violations from <short SHA>' --body '<which tools fired, which
      files changed>'`. The wrapper auto-resolves `--base` from
      origin's default branch.
   d. Log `outcome: ok, increment_signal: true, summary: <PR url>`.
3. Run remaining linters in check-only mode for issues that can't be
   auto-fixed. If any errors remain, open a separate PR with TODO comments
   placed at violation sites and a checklist.
4. If everything was already clean, log `outcome: ok, increment_signal: false`.


## Self-evolution
(self-evolve not enabled for this routine — your config is fixed by the user. Do not write to `evolve_requests.jsonl`.)

## Reference

All universal rules — commit/branch/push/PR procedure, `.iteration/log.jsonl`
line format, state-handling (which states fire vs. noop), failure modes,
and the mid-run evolve-request shape — live in the **shared preamble**:

  - `.claude/skills/_shared/preamble.md`

That file is rendered once at install (SKILL.md step 6f) from
`templates/routine-preamble.md`, identical bytes across every routine.
**Read it at the start of every fire** before producing work — it's
the canonical contract you commit / log / handle state against.

If a rule in this per-routine SKILL.md contradicts the shared preamble,
the preamble wins. Per-routine SKILL.md only adds *routine-specific*
content (purpose, trigger, prompt body); never re-declares universal
rules.
