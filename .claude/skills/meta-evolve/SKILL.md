---
name: meta-evolve
description: When .iteration/goal.md changes, re-plan the iteration's task list and commit the new breakdown. — installed by auto-routines on 2026-05-11T13:57:34+02:00, iter-1. Invoked by git-hook trigger (on every commit that touches .iteration/goal.md).
---

# meta-evolve

## Purpose
When .iteration/goal.md changes, re-plan the iteration's task list and commit the new breakdown.

## Trigger
on every commit that touches .iteration/goal.md

## Success criterion
tasks.md regenerated within 1 fire of every goal.md edit

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- `.iteration/goal.md` (the just-edited PRD — required; this is what changed).
- `git show HEAD -- .iteration/goal.md` to see exactly which lines moved.
- `.iteration/tasks.md` (the cached task breakdown you'll rewrite — may not exist on first fire; create it then).
- `gh pr list --state open --search 'head:routines/prd-implement'` (in-flight prd-implement PRs to preserve — do NOT rip out tasks that already have an open PR).

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

The user just edited the iteration goal. Your job is to re-plan the
task list so prd-implement (and any other goal-consuming routine)
sees the new shape on its next fire. You write the rewritten tasks
AND commit them — never analysis-only.

1. Read the new goal:
   - `.iteration/goal.md` is the canonical PRD/roadmap. Required.
     If missing, log `outcome: err, summary: "no goal at .iteration/goal.md"`
     and exit.
   - `git show HEAD -- .iteration/goal.md` to see exactly what
     changed in the triggering commit.
2. Read the prior task breakdown (if any):
   - `.iteration/tasks.md` — the cached ordered task list other
     routines consume. May not exist on first fire.
3. Decide what changed at the slice level:
   - Which existing tasks are now obsolete (goal removed them)?
   - Which existing tasks survive but need re-wording?
   - Which new tasks does the goal imply that aren't on the list?
   - For each "in progress" task (referenced by an open
     `routines/prd-implement` PR — `gh pr list --state open
     --search "head:routines/prd-implement"`), preserve it; do NOT
     rip out work in flight.
4. Rewrite `.iteration/tasks.md`:
   - 5–15 ordered tasks, smallest valuable unit first.
   - Each task: one line, imperative ("add X", "extract Y", "test Z").
   - Keep tasks already crossed off as `- [x]`; preserve their
     ordering at the top.
   - New tasks go at the bottom in dependency order.
5. Commit + PR:
   - Branch: `git checkout -B routines/meta-evolve`.
   - Commit with message
     `chore(plan): re-plan tasks after goal.md change at <short SHA>`.
   - Push and open a PR via `python3 scripts/orchestrator.py open-pr
     --head routines/meta-evolve --title 'chore(plan): re-plan tasks
     after goal.md change at <short SHA>' --body '<what changed in
     goal.md, tasks added, tasks removed, tasks reworded>'`. The
     wrapper auto-resolves `--base` from origin's default branch.
   - Hard cap: 10 minutes. If you can't make sense of the new
     goal, commit a minimal rewrite with TODO markers and a
     `WIP:` prefix; the next prd-implement fire will refine.
6. Log:
   - `increment_signal: true` if tasks.md actually changed.
   - `increment_signal: false` if you concluded the existing
     breakdown still matches the new goal (rare but possible).


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
