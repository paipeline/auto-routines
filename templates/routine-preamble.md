# auto-routines shared preamble

Single source of truth for the routine-agnostic mechanics every routine
needs: the FSM, the `log.jsonl` output schema, the `automation_level`
dispatch table, the PR recipe, the self-evolve schema, and the failure
modes.

Per-routine SKILL.md files at `.claude/skills/<routine_id>/SKILL.md` carry
only the routine-specific bits (purpose, trigger, prompt body) and point
here for the rest. Read this file *only if* your prompt body asks you to
consult the FSM, output format, PR recipe, self-evolve, or failure modes.
Most fires never need to.

This file is installed once per repo at `.claude/skills/_shared/preamble.md`
by the renderer. It contains no template placeholders — it is the same
literal content for every install. Edit `templates/routine-preamble.md` in
the skill source to change it.

---

## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/<routine-id>` (the `-B` resets
   the branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch:
   `git push -u origin routines/<routine-id> --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this
   branch; never use plain `--force`).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/<routine-id> \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/<routine-id> --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/<routine-id>-<ISO ts>.md` instead of committing.
Include a unified diff in the proposal so the user can apply it with
`git apply`.

If `automation_level: notify`: print findings only. No file writes outside
`.iteration/log.jsonl`.

If `automation_level: off`: you should not have been invoked. Log
`outcome: noop, summary: "skipped — automation_level=off"` and exit.

## Outputs

Append exactly one JSON line to `.iteration/log.jsonl` per fire:

```json
{
  "ts": "<iso8601 — local time with offset, e.g. 2026-05-09T17:03:00-0700; never UTC `Z`>",
  "routine": "<your routine id>",
  "outcome": "ok|noop|warn|err",
  "summary": "<one line — include PR url if you opened one>",
  "increment_signal": true,
  "last_fire_sha": "<git rev-parse HEAD>"
}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z` (NOT `date -u`, never UTC).
Logs are read by humans on their local machine — UTC `Z` makes them
unreadable without mental arithmetic. Cron is also local time per the
`scheduled-tasks` MCP, so log times match the schedule the user sees.

`increment_signal` MUST be `true` exactly when you produced something
useful (a commit, a PR, a comment, a fix, a generated test, a doc update).
The orchestrator uses this for stagnation detection — flat
`increment_signal: false` for `meta.stagnation_threshold` runs transitions
your routine to `STAGNANT`.

## Self-evolution (mid-run evolve request)

Routines whose config marks `self_evolve: true` may file a mid-run evolve
request if they decide their own config is wrong (too frequent, too rare,
scope drift, no longer useful). Append one JSON line to
`.iteration/evolve_requests.jsonl`:

```json
{"ts":"<local ISO8601 with offset>","routine_id":"<your id>","reason":"<one sentence>","suggested":"<one sentence>"}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z`. The always-on `Stop` hook
fires `/auto-routines evolve` at the end of the next Claude session, which
drains the file.

If your config has `self_evolve: false`, do NOT write to
`evolve_requests.jsonl`. Your config is fixed by the user.

## State handling

Every routine carries one of these FSM states in `.iteration/config.yaml`:

- `PROPOSED` — interview-stage, not yet running
- `ACTIVE` — running normally
- `EVOLVING` — running, but the user (or self-evolve) has flagged it for tuning
- `STAGNANT` — `increment_signal: false` for `meta.stagnation_threshold`
  consecutive fires; orchestrator skips dispatch
- `COMPLETED` — `success_criterion` met; archived
- `STOPPED` — manually disabled by the user

Read your current `state` from `.iteration/config.yaml`. If your state is
anything other than `ACTIVE` or `EVOLVING` when you fire, log
`outcome: noop, summary: "skipped — state=<state>"` and exit immediately.
Only `ACTIVE` and `EVOLVING` should produce work.

State transitions:

```
  PROPOSED ──user-confirm──► ACTIVE ──user-evolve-request──► EVOLVING
                              │ │                                │
                              │ └─stagnation_threshold──► STAGNANT
                              │                                  │
                              ├─success_criterion-met──► COMPLETED
                              └─user-disable──► STOPPED   (any state)
```

## Failure modes

- If a required dep (`gh`, an MCP, a CLI tool) is missing, log
  `outcome: err, summary: "missing dep: <name>"` and exit. The orchestrator
  reads these and may halt or retune your config.
- If your work hits the time budget without finishing, commit what you
  have with a `WIP:` prefix and a TODO checklist in the PR body. Better
  partial than nothing.
- Never silently swallow an exception. Always log to `log.jsonl` before
  exiting on error.
- If `gh` returns a 4xx/5xx during PR creation, retry once with a 5-second
  backoff, then log `outcome: err, summary: "gh api failed: <status>"` and
  exit. Do not loop indefinitely.
