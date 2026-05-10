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
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
{{routine_specific_inputs}}

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

{{routine_prompt_body}}

## Reference

If your prompt body asked you to consult the FSM, the `log.jsonl` output
schema, the `automation_level` dispatch table, the PR/commit recipe, the
self-evolve schema, or the failure-mode rules — read
`.claude/skills/_shared/preamble.md` once. Most fires never need to.
