---
name: session-doc-drift
description: Weekly, update README/SKILL.md/catalog when they diverge from code. — installed by auto-routines on 2026-05-11T13:57:34+02:00, iter-1. Invoked by scheduled trigger (5:00 PM Mondays).
---

# session-doc-drift

## Purpose
Weekly, update README/SKILL.md/catalog when they diverge from code.

## Trigger
5:00 PM Mondays

## Success criterion
docs in sync with code for 7 consecutive sessions

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- `README.md`, `SKILL.md`, `templates/routine-catalog.yaml`, `templates/routine-skill.md` (the docs that must stay in sync).
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
