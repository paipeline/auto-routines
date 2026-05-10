---
name: prd-implement
description: On a schedule, pick the next unimplemented slice from .iteration/goal.md, design it, write code+tests, commit and open a PR. — installed by auto-routines on 2026-05-10T19:13:25+02:00, iter-1. Invoked by scheduled trigger (every 12 hours).
---

# prd-implement

## Purpose
On a schedule, pick the next unimplemented slice from .iteration/goal.md, design it, write code+tests, commit and open a PR.

## Trigger
every 12 hours

## Success criterion
all tasks in .iteration/goal.md marked done

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state` (see below).
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- `.iteration/goal.md` (the canonical PRD — required).
- `.iteration/tasks.md` (cached task breakdown, if present).
- `gh pr list --state all --search 'head:routines/prd-implement' --limit 20` (your own past PRs, to avoid double-implementing).
- For self-hosted (this repo): `/tmp/auto-routines-test/iter-NNN-<slice>/` is a temp repo you may create to validate a change end-to-end before opening the PR. Tear it down on success; preserve on failure and reference the path in the PR body.

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

You are the **implementer**. Your job is to push the project forward
by exactly one concrete slice per fire. You read the goal document,
decide the next smallest valuable unit of work, write the code AND
the tests, and ship a PR. You DO NOT plan-only. You DO NOT print
findings. You write code.

1. Read the goal:
   - `.iteration/goal.md` is the canonical PRD/roadmap. Required.
     If missing, log `outcome: err, summary: "no goal at .iteration/goal.md"` and exit.
   - `.iteration/tasks.md` if present (cached task breakdown from a
     previous fire — incrementally updated, not authoritative).
2. Read completion state:
   - `git log --oneline` since your `last_fire_sha`.
   - Open and merged PRs from this routine:
     `gh pr list --state all --search "head:routines/prd-implement" --limit 20`.
   - Tasks already crossed off in `.iteration/tasks.md`.
3. Plan ahead — pick the next slice:
   - Smallest unit that delivers user-visible value (one endpoint,
     one component, one config option, one PRD bullet).
   - Skip anything with an open `routines/prd-implement` PR not yet
     merged — don't double-implement.
   - If `.iteration/tasks.md` does not exist, derive 5–10 ordered
     tasks from the goal document and write the file as your first
     output of this fire, then pick task #1.
   - If every task is done, log
     `outcome: ok, summary: "PRD complete", increment_signal: false`
     — the meta-agent will transition you to COMPLETED.
4. Design briefly (in your head — do NOT write a design doc):
   - What files to touch.
   - What tests to add (TDD: red → green → refactor).
   - What the user-visible change is.
5. Implement:
   - Write the failing test(s) FIRST. Run them. Confirm they fail
     for the right reason.
   - Write the minimum code to make them pass. Run again — green.
   - Run the full test suite. If anything else broke, fix it before
     continuing. Never commit broken tests as green.
   - Hard cap: 30 minutes total. If you hit the cap, commit what
     you have with a `WIP:` prefix and a TODO checklist in the PR.
6. Commit + PR:
   - Branch: `git checkout -B routines/prd-implement`.
   - Commit with conventional message
     (`feat:` / `fix:` / `refactor:` — one commit per logical change).
   - Push: `git push -u origin routines/prd-implement --force-with-lease`.
   - Open PR via `gh pr create` referencing the PRD section by line
     number. Body: what was built, why this slice next, test
     results, screenshots if UI.
7. Update `.iteration/tasks.md`:
   - Mark the slice you implemented as `[x] done — <PR url>`.
   - Commit the tasks update on the same branch (separate commit).
8. Log to `.iteration/log.jsonl`:
   - `outcome: ok, increment_signal: true, summary: "<PR url> — <slice title>"`
   - Set `increment_signal: false` only if you produced no diff
     (e.g. PRD was complete or every candidate slice was blocked).
9. Hard rules:
   - Never push to main. Never amend others' commits.
   - One PR per fire — never bundle multiple slices.
   - If you cannot decide what's next, ask via
     `.iteration/evolve_requests.jsonl` (append a request with
     `reason: "ambiguous next slice"`) — do not guess.


## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/prd-implement` (the `-B` resets the
   branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch: `git push -u origin routines/prd-implement --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this branch).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/prd-implement \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/prd-implement --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/prd-implement-<ISO ts>.md` instead of committing. Include
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
  "routine": "prd-implement",
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
You may file a mid-run evolve request if you decide your own config is wrong
(too frequent, too rare, scope drift, no longer useful). Append one JSON line
to `.iteration/evolve_requests.jsonl`:

```json
{"ts":"<local ISO8601 with offset>","routine_id":"<your id>","reason":"<one sentence>","suggested":"<one sentence>"}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z`. The always-on `Stop` hook
fires `/auto-routines evolve` at the end of the next Claude session, which
drains the file.


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
