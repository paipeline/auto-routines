---
name: prd-implement
description: On a schedule, pick the next unimplemented slice from .iteration/goal.md, design it, write code+tests, commit and open a PR. — installed by auto-routines on 2026-05-11T13:57:34+02:00, iter-1. Invoked by scheduled trigger (every 12 hours).
---

# prd-implement

## Purpose
On a schedule, pick the next unimplemented slice from .iteration/goal.md, design it, write code+tests, commit and open a PR.

## Trigger
every 12 hours

## Success criterion
all tasks in .iteration/goal.md marked done

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
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
   - Open PR via `python3 scripts/orchestrator.py open-pr
     --head routines/prd-implement --title '<conventional-commit
     summary>' --body '<what was built, why this slice next, PRD
     section by line number, test results, screenshots if UI>'`.
     The wrapper auto-resolves `--base` from origin's default branch.
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


## Self-evolution
You may file a mid-run evolve request if you decide your own config is wrong
(too frequent, too rare, scope drift, no longer useful). Append one JSON line
to `.iteration/evolve_requests.jsonl`:

```json
{"ts":"<local ISO8601 with offset>","routine_id":"<your id>","reason":"<one sentence>","suggested":"<one sentence>"}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z`. The always-on `Stop` hook
fires `/auto-routines evolve` at the end of the next Claude session, which
drains the file.


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
