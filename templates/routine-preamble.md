# Shared routine preamble — auto-routines

> This file is **rendered once** to `.claude/skills/_shared/preamble.md`
> by `init` / `evolve` (SKILL.md step 6f). Every per-routine SKILL.md
> references it via `## Reference`. The bytes are identical across
> every install, so the contents are likely to be cached when multiple
> routines fire in close succession — saving ~3KB of context per fire
> compared to inlining this block into every routine's SKILL.md.
>
> Rules in this file apply to **every** routine, regardless of
> primitive (scheduled, hook, git-hook, loop, pr-poll). If a rule is
> per-routine, it belongs in `templates/routine-skill.md`, not here.

## You MUST commit and open a PR for any work produced

If your routine's `automation_level: auto` (the default) and you
produced any code change:

1. Create a branch: `git checkout -B routines/<your-routine-id>` (the
   `-B` resets the branch every fire so you don't accumulate stale
   commits across runs). Look up `<your-routine-id>` from
   `.iteration/config.yaml` — your entry under `routines:`.
2. Stage and commit your changes with a clear, conventional message
   (`feat:`, `fix:`, `test:`, `docs:`, `style:`, `chore:`). One commit
   per logical change.
3. Push the branch:
   `git push -u origin routines/<your-routine-id> --force-with-lease`
   (force-with-lease is safe here because nothing else writes to this
   branch).
4. Open or update the PR:
   ```bash
   gh pr create \
       --base "$(git symbolic-ref --short refs/remotes/origin/HEAD | sed 's@^origin/@@')" \
       --head routines/<your-routine-id> \
       --title "<conventional summary>" \
       --body  "<one-paragraph why, then a checklist of what changed>" \
     || gh pr edit routines/<your-routine-id> \
            --body "<refreshed body>"
   ```
5. **Never push to main. Never force-push to a branch other than
   your own.** `--force-with-lease` is your friend; `--force` is not.

If `automation_level: suggest`: write your proposed change to
`.iteration/proposals/<your-routine-id>-<ISO ts>.md` instead of
committing. Include a unified diff in the proposal so the user can
apply it with `git apply`.

If `automation_level: notify`: print findings only. No file writes
outside `.iteration/log.jsonl`.

If `automation_level: off`: you should not have been invoked. Log
`outcome: noop, summary: "skipped — automation_level=off"` and exit.

## Outputs — `.iteration/log.jsonl`

Append exactly one JSON line to `.iteration/log.jsonl` per fire.
Canonical shape:

```json
{
  "ts": "<iso8601 — local time with offset, e.g. 2026-05-09T17:03:00-0700; never UTC `Z`>",
  "routine": "<your-routine-id>",
  "outcome": "ok|noop|warn|err",
  "summary": "<one line — include PR url if you opened one>",
  "increment_signal": true,
  "last_fire_sha": "<git rev-parse HEAD>"
}
```

Generate `ts` with `date +%Y-%m-%dT%H:%M:%S%z` (NOT `date -u`). Logs
are read by humans on their local machine — UTC `Z` makes them
unreadable without mental arithmetic. Cron is also local time per the
`scheduled-tasks` MCP, so log times match the schedule the user sees.

`increment_signal` MUST be `true` exactly when you produced something
useful (a commit, a PR, a comment, a fix, a generated test, a doc
update). The meta-agent uses this for stagnation detection — flat
`increment_signal: false` for `stagnation_threshold` runs transitions
you to `STAGNANT` (see `scripts/orchestrator.py fsm-plan`).

## State handling — FSM

Every routine carries one of:
`PROPOSED | ACTIVE | EVOLVING | STAGNANT | COMPLETED | STOPPED`.

Read your current `state` from `.iteration/config.yaml`. If your state
is anything other than `ACTIVE` or `EVOLVING` when you fire, log
`outcome: noop, summary: "skipped — state=<state>"` and exit
immediately. Only `ACTIVE` and `EVOLVING` should produce work.

State transitions are owned by the `evolve` routine (see SKILL.md
`Mode: evolve`):

| From       | To         | Owned by                                 |
| ---------- | ---------- | ---------------------------------------- |
| PROPOSED   | ACTIVE     | install (after user confirms) / evolve   |
| ACTIVE     | STAGNANT   | evolve (deterministic — `fsm-plan`)      |
| ACTIVE     | COMPLETED  | evolve (LLM — success_criterion match)   |
| ACTIVE     | EVOLVING   | evolve (mid-run request applied)         |
| EVOLVING   | ACTIVE     | evolve (after retune)                    |
| EVOLVING   | STOPPED    | evolve (after stop request)              |
| STAGNANT   | ACTIVE     | evolve (LLM — reactivation signals)      |
| COMPLETED  | ACTIVE     | evolve (LLM — reactivation signals)      |
| *          | STOPPED    | terminal — never leaves                  |

Never transition your own state. Only `evolve` rewrites
`config.yaml > routines[].state`.

## Success criteria

A routine's `success_criterion` is what tells the orchestrator when to
transition you ACTIVE → COMPLETED — your work is done; stop firing.
The field is a sealed union: pick one of the structured `kind`s below
where you can, and fall back to `llm-narrative` only when the criterion
is genuinely unstructured prose.

```yaml
success_criterion:
  kind: all-tasks-checked         # structured — orchestrator-enforced
  args:
    file: .iteration/goal.md
```

The orchestrator handles these kinds:

- `all-tasks-checked` — reads `args.file` (default `.iteration/goal.md`)
  and counts `[x]` vs `[ ]` markdown checkboxes. COMPLETED when every
  checkbox is checked AND there is at least one. Empty files never
  complete (otherwise a fresh install would auto-shut-down every
  routine before the user wrote their goal).
- `coverage-above` — reads `args.file` (default `coverage.xml`, format
  auto-detected: Cobertura XML or `coverage report` stdout) and
  compares the overall line-rate against `args.threshold` (percent,
  default 80, inclusive at the boundary). Missing/unparseable file
  returns false.
- `pr-merged-count` — counts entries in `.iteration/log.jsonl`
  belonging to this routine with `outcome: ok` AND a `pr_url` field.
  COMPLETED when the count meets or exceeds `args.count`. Scoped by
  routine id so other routines' PRs don't contribute.
- `no-failures-n-days` — scans this routine's log entries in the
  trailing `args.days` window. COMPLETED iff at least one entry falls
  in the window AND none has `outcome: err`. The "at least one in
  window" gate prevents auto-completing on an empty install.
- `llm-narrative` — fallback for unstructured criteria. The
  meta-agent reads `args.prose` and decides. Backward compat: an
  older string-valued `success_criterion` is auto-wrapped into
  `{kind: llm-narrative, args: {prose: <text>}}` at load time, so no
  config edit is required to upgrade.

The validator (`scripts/sanity-check.py`) rejects any other `kind`.
Add a new kind by editing `PREDICATE_KINDS` in both `sanity-check.py`
and `scripts/orchestrator.py`, adding an evaluator branch in
`evaluate_success_criterion()`, and updating this section — the
drift detector in `tests/test_preamble_predicates_matches_sanity.py`
pins all three surfaces together.

## Failure modes

- **Missing dep** (`gh`, an MCP, a CLI tool not on PATH): log
  `outcome: err, summary: "missing dep: <name>"` and exit. The
  `evolve` routine reads these and may halt or retune your config.
- **Time budget exceeded**: if your work hits the time budget without
  finishing, commit what you have with a `WIP:` prefix and a TODO
  checklist in the PR body. Better partial than nothing.
- **Never silently swallow an exception.** Always log to
  `.iteration/log.jsonl` before exiting on error — the dashboard and
  status command both depend on the log line to surface failures.
- **Never write outside your sandbox.** Allowed write targets:
  - `.iteration/log.jsonl` (append)
  - `.iteration/proposals/<your-routine-id>-*.md` (suggest mode)
  - Any code path your routine is supposed to touch (per its prompt)
  - Your routine's own branch (`routines/<your-routine-id>`)
  Never touch `.iteration/config.yaml > routines[]` from a routine —
  that's `evolve`'s exclusive turf.

## Self-evolution — mid-run evolve requests

If during your fire you notice your own configuration is wrong
(too-frequent cron, useless prompt body, missing dependency the user
must install), append one line to `.iteration/evolve_requests.jsonl`:

```json
{
  "ts": "<iso8601 local>",
  "routine_id": "<your-routine-id>",
  "reason": "<one-line plain-English>",
  "suggested": "retune | stop | expand | <free text>"
}
```

The next `evolve` fire drains the file (see
`scripts/orchestrator.py drain-evolve-requests`) and applies your
suggestion. Do this at most once per fire; spamming requests gets
your routine marked STAGNANT faster.
