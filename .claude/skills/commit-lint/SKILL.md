---
name: commit-lint
description: Run linters after every commit; auto-fix and PR when violations exist. — installed by auto-routines on 2026-05-09T22:31:14+02:00, iter-1. Invoked by git-hook trigger (on every git commit).
---

# commit-lint

## Purpose
Run linters after every commit; auto-fix and PR when violations exist.

## Trigger
on every git commit

## Success criterion
0 lint violations in last 20 commits

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state` (see below).
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- - `git diff HEAD~1 HEAD` for the changed files.
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
   c. Open PR via `gh pr create`. Body lists which tools fired and which
      files changed.
   d. Log `outcome: ok, increment_signal: true, summary: <PR url>`.
3. Run remaining linters in check-only mode for issues that can't be
   auto-fixed. If any errors remain, open a separate PR with TODO comments
   placed at violation sites and a checklist.
4. If everything was already clean, log `outcome: ok, increment_signal: false`.


## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/commit-lint` (the `-B` resets the
   branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch: `git push -u origin routines/commit-lint --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this branch).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/commit-lint \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/commit-lint --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/commit-lint-<ISO ts>.md` instead of committing. Include
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
  "routine": "commit-lint",
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
