---
name: session-doc-drift
description: On weekday evenings, update README/SKILL.md/catalog when they diverge from code. — installed by auto-routines on 2026-05-09T19:24:44+02:00, iter-1. Invoked by scheduled trigger (5:00 PM weekdays).
---

# session-doc-drift

## Purpose
On weekday evenings, update README/SKILL.md/catalog when they diverge from code.

## Trigger
5:00 PM weekdays

## Success criterion
docs in sync with code for 7 consecutive sessions

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state` (see below).
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- - `README.md`, `SKILL.md`, `templates/routine-catalog.yaml`, `templates/routine-skill.md` (the docs that must stay in sync).
- `git diff` of the session against these files to spot which doc has fallen behind code.

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

The Claude session just ended. Your job:

1. Identify "documented surface" files: README.md, docs/**, plus any
   file in `.iteration/config.yaml > docs.tracked_paths` if present.
2. Identify "documented things":
   - CLI commands shown in README → check they exist and produce the
     shown output (run them with --help, diff against the README).
   - API endpoints / function signatures → grep code for the signature,
     confirm it matches.
   - Install steps → verify each command is still in package.json /
     pyproject.toml / Makefile.
3. For each drift:
   a. Update the doc to match code. Never update code to match docs
      (that's the user's job — flag those instead).
   b. If the README references a feature that was deleted, remove the
      reference and note it in the PR body.
4. If you changed any doc:
   a. Branch: `routines/session-doc-drift`. Commit with message
      `docs: sync README with code (<short summary>)`.
   b. PR body: bullet list of each drift fixed, with file:line refs.
   c. Log `outcome: ok, increment_signal: true, summary: <PR url>`.
5. If no drift, log `outcome: ok, increment_signal: false`.


## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/session-doc-drift` (the `-B` resets the
   branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch: `git push -u origin routines/session-doc-drift --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this branch).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/session-doc-drift \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/session-doc-drift --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/session-doc-drift-<ISO ts>.md` instead of committing. Include
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
  "routine": "session-doc-drift",
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
