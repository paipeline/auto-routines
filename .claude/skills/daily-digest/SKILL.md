---
name: daily-digest
description: Summarize today's commits, PRs, and routine activity into .iteration/digests/. — installed by auto-routines on 2026-05-11T13:57:34+02:00, iter-1. Invoked by scheduled trigger (6:00 PM daily).
---

# daily-digest

## Purpose
Summarize today's commits, PRs, and routine activity into .iteration/digests/.

## Trigger
6:00 PM daily

## Success criterion
(none — runs indefinitely)

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- `git log --since="00:00 today" --pretty=format:"%h %s (%an)"`
- `gh pr list --state all --search "updated:>$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)"`
- Tail of `.iteration/log.jsonl` since 00:00 today.

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

Write a one-page digest of today's repo activity.

0. Budget check (token-frugality path, PRD goal.md):
   - Read `meta.budget` from `.iteration/config.yaml`.
   - If `meta.budget` is `low` or `medium`:
       - Run `bash scripts/daily-digest.sh "00:00 today" > .iteration/digests/$(date +%Y-%m-%d).md`.
       - Commit the digest on `routines/daily-digest`. Then open a
         PR via `python3 scripts/orchestrator.py open-pr
         --head routines/daily-digest --title 'chore(digest):
         <YYYY-MM-DD> daily digest' --body '<digest body — top
         commits, PR activity, routine outcomes>'` (or push
         directly to main if `config.yaml > daily_digest.push_direct:
         true`). The wrapper auto-resolves `--base` from origin's
         default branch.
       - Log outcome with `increment_signal: true` and EXIT — do not
         run the LLM flow below. The whole point of low/medium is
         zero Claude tokens for the digest body.
   - If `meta.budget` is `high` or `custom`: continue with the LLM flow.

1. Gather:
   - `git log --since="00:00 today" --pretty=format:"%h %s (%an)"`
   - `gh pr list --state all --search "updated:>$(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)"`
   - Tail of `.iteration/log.jsonl` since 00:00.
2. Write `.iteration/digests/<YYYY-MM-DD>.md` with sections:
   ## Commits   — bullet list with one-line context
   ## PRs       — opened, merged, still-open-failing
   ## Routines  — what fired, what was useful, what was noisy
   ## Tomorrow  — top 3 things blocking progress (your judgment)
3. Commit the digest on branch `routines/daily-digest`. Then open a
   PR via `python3 scripts/orchestrator.py open-pr
   --head routines/daily-digest --title 'chore(digest): <YYYY-MM-DD>
   daily digest' --body '<digest body — summarize the four sections
   above>'` (or push directly to main if
   `config.yaml > daily_digest.push_direct: true`). The wrapper
   auto-resolves `--base` from origin's default branch.
4. Log outcome with `increment_signal: true` if there was any activity
   today, false on empty days.


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
