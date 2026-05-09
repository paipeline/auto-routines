---
name: {{routine_id}}
description: {{purpose}} — installed by auto-routines on {{installed_at}}, iter-{{iter_added}}. Invoked by {{primitive}} trigger ({{trigger_summary}}).
---

# {{routine_id}}

## Purpose
{{purpose}}

## Trigger
{{trigger_summary}}

## Success criterion
{{success_criterion}}

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state` (see below).
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- {{routine_specific_inputs}}

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

{{routine_prompt_body}}

## You MUST commit and open a PR for any work produced

If `automation_level: auto` (the default) and you produced any code change:

1. Create a branch: `git checkout -B routines/{{routine_id}}` (the `-B` resets the
   branch every fire so you don't accumulate stale commits across runs).
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit per
   logical change.
3. Push the branch: `git push -u origin routines/{{routine_id}} --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this branch).
4. Open or update the PR:
   ```
   gh pr create --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/{{routine_id}} \
       --title "<conventional summary>" \
       --body "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/{{routine_id}} --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than your own.**

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/{{routine_id}}-<ISO ts>.md` instead of committing. Include
a unified diff in the proposal so the user can apply it with `git apply`.

If `automation_level: notify`: print findings only. No file writes outside
`.iteration/log.jsonl`.

If `automation_level: off`: you should not have been invoked. Log
`outcome: noop, summary: "skipped — automation_level=off"` and exit.

## Outputs
Append exactly one JSON line to `.iteration/log.jsonl` per fire:

```json
{
  "ts": "<iso8601>",
  "routine": "{{routine_id}}",
  "outcome": "ok|noop|warn|err",
  "summary": "<one line — include PR url if you opened one>",
  "increment_signal": true,
  "last_fire_sha": "<git rev-parse HEAD>"
}
```

`increment_signal` MUST be `true` exactly when you produced something useful
(a commit, a PR, a comment, a fix, a generated test, a doc update). The meta-
agent uses this for stagnation detection — flat `increment_signal: false` for
`stagnation_threshold` runs transitions you to `STAGNANT`.

## Self-evolution (mid-run evolve request)
{{self_evolve_block}}

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
