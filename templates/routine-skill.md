---
name: {{routine_id}}
description: {{purpose}} — installed by auto-routines on {{installed_at}}, iter-{{iter_added}}. Invoked by {{primitive}} trigger ({{trigger_summary}}).
---

# {{routine_id}}

## Purpose
{{purpose}}

## Trigger
{{trigger_summary}}

## Inputs to read at fire time
- `.iteration/config.yaml` (your own entry under `routines:` — read `automation_level`)
- Recent `git log` since last fire of this routine
- {{routine_specific_inputs}}

## What to do
{{routine_prompt_body}}

## Outputs
- Append a JSON line to `.iteration/log.jsonl`:
  `{"ts": "<iso8601>", "routine": "{{routine_id}}", "outcome": "<ok|noop|warn|err>", "summary": "<one line>"}`
- If you took an action (commit, PR comment, file write), include the artifact in `summary` so `evolve` can score usefulness.

## Automation level handling
- `off` — refuse to fire (this skill should not be invoked when off).
- `notify` — only print findings, do not modify any file.
- `suggest` — open a PR or write a proposal file under `.iteration/proposals/{{routine_id}}-<ts>.md`.
- `auto` — apply changes directly, but always commit on a routine branch `routines/{{routine_id}}` then open a PR; never push to main.

## Failure modes
- If a required dep (gh, an MCP) is missing, log `outcome: err` with the missing dep, then exit. The `evolve` routine reads these and may halt.
