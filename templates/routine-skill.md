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
- `.iteration/config.yaml` (your own entry under `routines:` — read `automation_level` and `state`)
- Recent `git log` since last fire of this routine
- {{routine_specific_inputs}}

## What to do
{{routine_prompt_body}}

## Outputs
- Append a JSON line to `.iteration/log.jsonl`:
  `{"ts": "<iso8601>", "routine": "{{routine_id}}", "outcome": "<ok|noop|warn|err>", "summary": "<one line>", "increment_signal": <true|false>}`
- `increment_signal: true` if you produced something useful this run (commit, PR comment, fix). The meta-agent uses this for stagnation detection — flat `useful` for `stagnation_threshold` runs transitions you to STAGNANT.
- If you took an action (commit, PR comment, file write), include the artifact in `summary` so `evolve` can score usefulness.

## Self-evolution (mid-run evolve request)
{{self_evolve_block}}

## Automation level handling
- `off` — refuse to fire (this skill should not be invoked when off).
- `notify` — only print findings, do not modify any file.
- `suggest` — open a PR or write a proposal file under `.iteration/proposals/{{routine_id}}-<ts>.md`.
- `auto` — apply changes directly, but always commit on a routine branch `routines/{{routine_id}}` then open a PR; never push to main.

## State handling
This routine carries one of: ACTIVE | EVOLVING | STAGNANT | COMPLETED | STOPPED. If your `state` is anything other than ACTIVE or EVOLVING when you fire, log `outcome: noop` with `summary: "skipped — state=<state>"` and exit immediately. Only `ACTIVE` and `EVOLVING` should produce work.

## Failure modes
- If a required dep (gh, an MCP) is missing, log `outcome: err` with the missing dep, then exit. The `evolve` routine reads these and may halt.
