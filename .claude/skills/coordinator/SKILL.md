---
name: coordinator
description: Central agent: read the brief and decide which routine(s) to dispatch this fire. — installed by auto-routines on 2026-05-09T22:31:14+02:00, iter-3. Invoked by scheduled trigger (every 12 hours).
---

# coordinator

## Purpose
Central agent: read the brief and decide which routine(s) to dispatch this fire.

## Trigger
every 12 hours

## Success criterion
PRD complete + zero stagnant routines

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state` (see below).
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- - `python3 scripts/coordinator-brief.py` (the structured brief — **always** run this first; it's pure shell, no LLM tokens).
- `.iteration/goal.md` (only after the brief flags PRD as the lever).
- `.claude/skills/<routine_id>/SKILL.md` for the routine you decide to dispatch — read it then, not preemptively.

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

You are the **coordinator** — the central agent. Each fire, you decide
what (if anything) the auto-routines system should do *right now*, then
execute that decision in this same session. There is no separate
scheduled task per routine; you are the only timer.

## 1. Generate the brief (no LLM tokens)

Run the pure-shell brief script and capture its output:

```bash
python3 scripts/coordinator-brief.py
```

The brief tells you:
- PRD progress (done/todo + next 3 unchecked slices from `.iteration/goal.md`)
- Routine roster (id, state, last fire, runs/useful/noisy)
- Open routine PRs (anything on a `routines/*` branch awaiting merge)
- Recent commits since your last fire
- Pending evolve requests
- Your own last 5 decisions (for stagnation detection)

Read it. Do not skip. The brief is what makes this session cheap —
without it you'd be groveling through `git log` and `gh pr list` yourself.

## 2. Decide

Pick exactly one of these actions for this tick:

**dispatch <routine_id>** — invoke a single routine. Reasons to do this:
- PRD has unchecked slices and no open `routines/prd-implement` PR
  → dispatch `prd-implement`
- It's been >7 days since `session-doc-drift` ran and the brief shows
  recent commits to README.md / SKILL.md / catalog
  → dispatch `session-doc-drift`
- It's the daily 6 PM window and `daily-digest` hasn't fired today
  → dispatch `daily-digest`
- Pending evolve requests > 0
  → dispatch `evolve` (the meta routine)

**dispatch <a> + <b>** — chain two routines (rare, only when both are
cheap and independent — e.g. digest + drift on the same tick).

**noop** — do nothing this fire. Reasons:
- PRD is complete and no other routines have signals
- An open `routines/prd-implement` PR is still awaiting CI/review —
  running again would create duplicate work
- The brief shows the last 3 coordinator fires were all `noop` AND
  the last `prd-implement` fire was `increment_signal: true` — give
  the system breathing room
- The system is `STAGNANT` per recent log signals — escalate via
  evolve_requests instead of dispatching more work

**escalate** — write a request to `.iteration/evolve_requests.jsonl`
and noop. Use when you cannot decide cleanly (e.g. PRD ambiguous,
conflicting signals, every routine looks blocked).

## 3. Execute

If you decided **dispatch**: read `.claude/skills/<routine_id>/SKILL.md`
and follow its instructions in this same session. The dispatched
routine logs its OWN entry to `.iteration/log.jsonl` (with its own
`routine: <id>` field) — that's how the brief tracks it next time.

If **noop** or **escalate**: skip the routine work entirely.

## 4. Log your decision (always — even on noop)

Append exactly one JSON line to `.iteration/log.jsonl`:

```json
{
  "ts": "<date +%Y-%m-%dT%H:%M:%S%z>",
  "routine": "coordinator",
  "outcome": "ok|noop|warn|err",
  "summary": "<action> — <one-sentence reason>",
  "increment_signal": <true if you dispatched a routine that produced work, else false>,
  "last_fire_sha": "<git rev-parse HEAD>"
}
```

The `summary` must name the action: `"dispatched prd-implement — 3 slices left, no open PR"`,
`"noop — open routines/prd-implement PR #42 awaiting merge"`, etc.

Future-you reads these via the brief — make them informative.

## 5. Hard rules
- One decision per fire. Never dispatch >2 routines in one tick.
- Never bypass the brief. The brief is the input; the LLM does NOT
  re-derive state from logs/PRs/git directly.
- Never push to main. Dispatched routines branch on their own
  `routines/<id>`.
- If `scripts/coordinator-brief.py` is missing or fails, log
  `outcome: err, summary: "brief script broken"` and exit. The
  meta-evolve will pick this up.


## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/coordinator` (the `-B` resets the
   branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch: `git push -u origin routines/coordinator --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this branch).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/coordinator \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/coordinator --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/coordinator-<ISO ts>.md` instead of committing. Include
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
  "routine": "coordinator",
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
