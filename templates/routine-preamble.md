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
