---
name: meta-evolve
description: When .iteration/goal.md changes, re-plan the iteration's task list and commit the new breakdown. — installed by auto-routines on 2026-05-10T19:49:22+02:00, iter-1. Invoked by git-hook trigger (on every commit that touches .iteration/goal.md).
---

# meta-evolve

## Purpose
When .iteration/goal.md changes, re-plan the iteration's task list and commit the new breakdown.

## Trigger
on every commit that touches .iteration/goal.md

## Success criterion
tasks.md regenerated within 1 fire of every goal.md edit

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state` (see below).
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
   - Push and open a PR via `gh pr create`. Body: "what changed in
     goal.md", "tasks added", "tasks removed", "tasks reworded".
   - Hard cap: 10 minutes. If you can't make sense of the new
     goal, commit a minimal rewrite with TODO markers and a
     `WIP:` prefix; the next prd-implement fire will refine.
6. Log:
   - `increment_signal: true` if tasks.md actually changed.
   - `increment_signal: false` if you concluded the existing
     breakdown still matches the new goal (rare but possible).


## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/meta-evolve` (the `-B` resets the
   branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch: `git push -u origin routines/meta-evolve --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this branch).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/meta-evolve \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/meta-evolve --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/meta-evolve-<ISO ts>.md` instead of committing. Include
a unified diff in the proposal so the user can apply it with `git apply`.

If `automation_level: notify`: print findings only. No file writes outside
`.iteration/log.jsonl`.

If `automation_level: off`: you should not have been invoked. Log
`outcome: noop, summary: "skipped — automation_level=off"` and exit.

## Outputs
Append exactly one JSON line to `.iteration/log.jsonl` per fire:

```json
{
  "ts": "<iso8601 — local time with offset, e.g. 2026-05-09T17:03:00-0700; never UTC `Z`>",
  "routine": "meta-evolve",
  "outcome": "ok|noop|warn|err",
  "summary": "<one line — include PR url if you opened one>",
  "increment_signal": true,
  "last_fire_sha": "<git rev-parse HEAD>"
}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z` (NOT `date -u`). Logs are
read by humans on their local machine — UTC `Z` makes them unreadable
without mental arithmetic. Cron is also local time per the
`scheduled-tasks` MCP, so log times match the schedule the user sees.

`increment_signal` MUST be `true` exactly when you produced something useful
(a commit, a PR, a comment, a fix, a generated test, a doc update). The meta-
agent uses this for stagnation detection — flat `increment_signal: false` for
`stagnation_threshold` runs transitions you to `STAGNANT`.

## Self-evolution (mid-run evolve request)
(self-evolve not enabled for this routine — your config is fixed by the user. Do not write to `evolve_requests.jsonl`.)

## State handling
This routine carries one of `ACTIVE | EVOLVING | STAGNANT | COMPLETED | STOPPED`.
Read your current `state` from `.iteration/config.yaml`. If your state is
anything other than `ACTIVE` or `EVOLVING` when you fire, log
`outcome: noop, summary: "skipped — state=<state>"` and exit immediately.
Only `ACTIVE` and `EVOLVING` should produce work.

## Failure modes
- If a required dep (`gh`, an MCP, a CLI tool) is missing, log
  `outcome: err, summary: "missing dep: <name>"` and exit. The `evolve`
  routine reads these and may halt or retune your config.
- If your work hits the time budget without finishing, commit what you have
  with a `WIP:` prefix and a TODO checklist in the PR body. Better partial
  than nothing.
- Never silently swallow an exception. Always log to `log.jsonl` before
  exiting on error.
