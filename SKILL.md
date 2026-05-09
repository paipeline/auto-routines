---
name: auto-routines
description: Analyze a repository, interview the user about goal, automation appetite, and per-routine frequency, then install and evolve a finite-state set of repo-specific routines (hooks, scheduled tasks, loops, PR-comment agents, git hooks). A daily meta-routine adapts the set based on commits, PRs, CI, and routine logs; routines themselves can request mid-run evolution. Routines transition through PROPOSED → ACTIVE → (EVOLVING/STAGNANT/COMPLETED) → STOPPED. Invoke when the user runs `/auto-routines [init|evolve|status|stop|start|revert]` or asks to set up / iterate project automations.
---

# auto-routines

You are operating the `auto-routines` skill. The user wants their repository to run a self-evolving set of automations. Interview them once with explicit per-routine frequency choices, install routines as a finite-state machine, and from then on the daily `evolve` mode plus mid-run evolve requests adapt the set autonomously while always showing a plain-text status block (am/pm times, no mermaid) and gating risky changes behind a sanity check.

## Modes

Detect mode from the user's invocation:

- **No args, no `.iteration/` exists** → `init`
- **No args, `.iteration/` exists** → `status`
- **`init`** → run full init flow (re-running re-interviews, but preserves history)
- **`evolve`** → run one iteration. Optional `--triggered-by <routine_id> --reason <text>` records the trigger.
- **`status`** → print the text status block (goal, mode, routines table with am/pm schedules and FSM states, pending evolve requests, last iter)
- **`stop <routine_id>`** → transition routine ACTIVE → STOPPED (neutralize the underlying task)
- **`start <routine_id>`** → transition routine STAGNANT|STOPPED → ACTIVE (un-neutralize, re-arm)
- **`revert <iter-NNN>`** → restore checkpoint

## Guardrails (apply to every mode)

1. **Always print the text status block inline** at the end of `init`, `evolve`, `status`, `stop`, `start`. Never use mermaid. Times must render as am/pm (e.g. `9:00 AM daily`, `5:00 PM weekdays`). Cron is internal; humans never see it in output unless they explicitly asked.
2. **Sanity check before any apply**: run `scripts/sanity-check.py .iteration/config.yaml` (or against a proposed config). If it fails, halt and surface the report. Never apply broken config.
3. **Checkpoint before any change in `evolve`/`stop`/`start`**: create a git commit `iter-NNN: <summary>` and record SHA in `.iteration/checkpoints.md`. The user must always be able to `revert` to the previous iter.
4. **Dependency health check**: at start of every mode, verify `gh auth status` (if `deps.gh != none`) and every MCP listed in `config.yaml > deps.mcps`. If any fail, write `.iteration/halted.md` with the failure and STOP. On next invocation, recheck — if healthy, delete `halted.md` and continue.
5. **Confirm before initial install**: after interview + render, show the proposed status block and ask the user to confirm. Subsequent `evolve` runs are fully auto (no confirm).
6. **Never write to `.iteration/` from inside `init` until the user confirms.** Stage proposals in `/tmp/auto-routines-staging-<repo_slug>/` until confirmed; sanity-check runs against the staged file.
7. **Ownership of scheduled tasks** — the `scheduled-tasks` MCP is per-user and shared across repos, and it sanitizes `taskId` to plain kebab-case. To make ownership reliable:
   - **`taskId` format**: `auto-routines-<repo_slug>-<routine_id>` (all lowercase, all hyphen-separated). Meta uses `auto-routines-<repo_slug>-meta`.
   - **Description is ground truth for ownership**: every task this skill creates MUST set its `description` to start with `[auto-routines:<repo_slug>]`.
   - **Store the actual `task_id` per routine in `config.yaml`** under `routines[].task_id`, captured from the MCP response. Never re-derive at use time.
   - **Before creating any task**: list and check whether the planned `taskId` already exists. If it does and is owned by us, reuse via `update_scheduled_task`; otherwise abort.
   - **Orphan detection**: list all tasks, filter to our prefix, diff against `config.yaml`. Anything in the listing not in config is an orphan — neutralize.
8. **Neutralize, don't delete (current MCP limitation).** The `scheduled-tasks` MCP exposes only `create`, `list`, `update`. To "remove" a task: `update_scheduled_task` with `enabled: false`, `cronExpression: "0 0 1 1 *"` (Jan-1-only), and a description rewritten to start with `[auto-routines:DELETED:<repo_slug>]`. Record under `config.yaml > neutralized_tasks`.
9. **Slug normalization** — computed once at `init` and stored. `basename` → lowercase → strip non `[a-z0-9-]` → collapse runs of `-` → strip leading/trailing `-` → strip leading `auto-routines-` → if empty/digit-leading prepend `r-` → truncate to 32 → validate.

## The finite state machine

Every routine carries `state`:

```
PROPOSED   shown in interview, not yet confirmed by the user
ACTIVE     installed, firing on its schedule
EVOLVING   transient — meta-agent is currently re-evaluating it
STAGNANT   stats.useful did not increase over last `stagnation_threshold` runs (re-openable)
COMPLETED  routine.success_criterion verified by meta (re-openable)
STOPPED    user-disabled or meta-removed (real terminal — task is neutralized)
```

Transitions (you, the meta-agent, are responsible for executing these during `evolve`):

```
PROPOSED   → ACTIVE       on user confirm in interview
ACTIVE     → COMPLETED    when meta verifies routine.success_criterion is met
ACTIVE     → STAGNANT     when stats.useful is flat for `stagnation_threshold` runs
ACTIVE     → EVOLVING     when a mid-run evolve request targets this routine (transient)
EVOLVING   → ACTIVE       after meta retunes (frequency/prompt/scope)
EVOLVING   → STOPPED      after meta decides to remove
ACTIVE     → STOPPED      on `/auto-routines stop <id>`
STAGNANT   → ACTIVE       when meta sees fresh signal in a later iter
COMPLETED  → ACTIVE       when criterion stops holding (rare)
STOPPED    → ACTIVE       only via explicit `/auto-routines start <id>` (resets stats)
```

`STOPPED` neutralizes the underlying task per Guardrail 8. `STAGNANT` and `COMPLETED` keep the task disabled (`enabled: false`) but do NOT rewrite the description prefix — they remain re-openable.

## Mode: `init`

1. **Preflight** — verify git work tree; check `gh auth status`; list connected MCPs.
2. **Compute identity** — `repo_slug` per Guardrail 9.
3. **Analyze** — detect stack (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, …); detect tests/CI; read `git log --oneline -50`; `gh pr list --state all --limit 20`; read `README*`.
4. **Interview** (use `AskUserQuestion` for every step):
   - **Goal**: "What's the goal of this project (one sentence)?"
   - **Mode**: pick `goal-driven` or `fully-auto`.
   - **MCPs**: multi-select from connected MCPs.
   - **Candidate routines**: based on analysis, propose 4–8 candidates.
   - **For each enabled candidate, ask three questions in order:**
     1. **Frequency** (multi-choice — never show cron in the UI):
        ```
        [1] every 15 minutes
        [2] every 30 minutes  (recommended)
        [3] every hour
        [4] twice daily — 9:00 AM and 5:00 PM
        [5] daily — 9:00 AM
        [6] weekdays only — 5:00 PM
        [7] on every git commit          (becomes primitive: git-hook)
        [8] custom — type your own       (free text, you parse)
        [skip] don't install this routine
        ```
        Internally store `trigger.cron` (canonical) AND `trigger.human` (the chosen label).
     2. **Success criterion** (free text, optional): "How will we know this routine has done its job? (leave blank if it should run indefinitely)" — stored as `routine.success_criterion`. Used by meta to transition ACTIVE → COMPLETED.
     3. **Self-evolve allowed?** (yes/no, default yes): "May this routine fire `/auto-routines evolve` mid-run if it notices its own config is wrong?" — stored as `routine.self_evolve`.
5. **Render & confirm** — stage `config.yaml`, sanity-check, render the text status block per the "Status display" section, ask user to confirm.
6. **Install** (only after confirm):
   - Move staged config into `.iteration/config.yaml`. Create `.iteration/log.jsonl`, `.iteration/checkpoints.md`, `.iteration/evolve_requests.jsonl` (empty), `.iteration/plan.txt`, `.iteration/history/iter-001-init.md`.
   - Pre-flight ownership check (Guardrail 7).
   - For each routine: install per "Wiring routines"; capture `task_id`; transition `state: PROPOSED → ACTIVE`.
   - Install the `Stop` hook that drains `.iteration/evolve_requests.jsonl` (see "Mid-run self-evolution").
   - For every `git-hook`: append hook output paths to `.gitignore`.
   - Schedule the meta-routine: `auto-routines-<repo_slug>-meta`, description `[auto-routines:<repo_slug>] meta — daily evolve`, prompt `cd <abs repo path> && /auto-routines evolve`. The `cd` is required.
   - Two-step commit: `git add .iteration .claude && git commit -m "iter-001: install auto-routines"` → record SHA in `checkpoints.md` → `git commit --amend --no-edit`.
   - Print the final status block.
   - Delete `/tmp/auto-routines-staging-<repo_slug>/`.

## Mode: `evolve`

Fully auto. Every change is checkpointed and sanity-checked.

1. **Health check** (Guardrail 4).
2. **Drain mid-run requests**: read `.iteration/evolve_requests.jsonl`, parse each entry. Mark all targeted routines `state: ACTIVE → EVOLVING` for this iter. After processing, truncate the file. Log every drained request in `iter-NNN.md`.
3. **Gather signals**:
   - `git log --since="<last-iter-time>"`
   - `gh pr list --state all --limit 30 --json number,state,title,statusCheckRollup`
   - Tail `.iteration/log.jsonl`
   - Read `.iteration/config.yaml`
   - `gh run list --limit 20` for CI failures
4. **Run automatic FSM transitions** before deciding new changes:
   - For each ACTIVE routine, check `success_criterion` against signals. If verifiably met → transition to COMPLETED, neutralize the schedule but keep `enabled: false` not the DELETED prefix (so it's re-openable).
   - For each ACTIVE routine, count runs since `stats.last_useful_iter`. If `>= stagnation_threshold` (fall back to `meta.default_stagnation_threshold`) → transition to STAGNANT, neutralize.
   - For each STAGNANT or COMPLETED routine, check if signals justify reactivation → transition back to ACTIVE.
5. **Decide** new ADD / REMOVE / RETUNE based on `mode`:
   - **goal-driven**: re-read iteration goal, propose routines that close the gap. If goal is met, write `.iteration/next-goal.md` and continue with current routines.
   - **fully-auto**: signals-driven (CI flake, PR queue depth, doc drift, commit cadence).
   - **Mid-run targets**: routines currently in EVOLVING state — apply the suggested action from the request (retune, stop, expand) and transition out of EVOLVING (→ ACTIVE or → STOPPED).
   - Anti-flap: skip any routine id removed within last `meta.anti_flap_window` iters.
6. **Sanity check** the proposed new config (write to `/tmp/...`, run `scripts/sanity-check.py`). On failure, write `.iteration/sanity-failed-NNN.md`, halt.
7. **Checkpoint** (two-step amend pattern as in `init`).
8. **Apply** diff-based:
   - **Add**: install per "Wiring routines"; capture `task_id`; `state: PROPOSED → ACTIVE`.
   - **Edit / Retune**: `update_scheduled_task` (reuse stored `task_id`); rewrite the per-routine SKILL.md if its prompt changed; update `trigger.human`.
   - **Remove**: neutralize per Guardrail 8; `state: → STOPPED`.
   - **Final reconciliation**: orphan sweep (Guardrail 7).
9. **Render** new `.iteration/plan.txt` per "Status display".
10. **Log** to `.iteration/history/iter-NNN.md` (must include the `triggered_by` value if invoked with `--triggered-by`) and `.iteration/log.jsonl`.
11. **Print** the status block + a one-paragraph summary of what changed and why.

## Mid-run self-evolution

A routine, while running, can decide its own config is wrong. Three paths:

**(a) Request file (default).** The routine ends by appending one JSON line to `.iteration/evolve_requests.jsonl`:
```json
{"ts":"2026-05-09T14:32:00-07:00","routine_id":"pr-ci-watcher","reason":"CI flake rate at 0% over last 200 PRs","suggested":"reduce frequency"}
```
The `Stop` hook installed at `init` time runs at the end of every Claude session — it tails the file, and if any unprocessed lines exist, it fires `claude -p "/auto-routines evolve"` (which will drain the requests in step 2 above). Async, low cost, no nested Claude during the routine itself.

**(b) Direct sub-invocation.** The routine, before it ends, runs:
```
claude -p "/auto-routines evolve --triggered-by <routine_id> --reason '<short text>'"
```
Used when the routine wants the change applied before its next fire. Heavier (spawns a sub-Claude), used sparingly. Generated routine prompts include this snippet only when `routine.self_evolve: true` AND the routine's purpose suggests it could detect its own irrelevance (e.g. doc-drift fixers, CI watchers).

**(c) User-fired.** The user types `/auto-routines evolve` in their session at any time. Same code path as the daily fire. The trigger reason is logged as `"manual"`.

The flag `routine.self_evolve` (default true) gates (a) and (b). Set false for routines you don't want firing the meta — typically routines whose value is intrinsic and not signal-dependent (e.g. a daily-digest that's just for the human).

## Mode: `status`

Print the text status block. No mermaid. No mode-specific work beyond reading state.

## Mode: `stop <routine_id>`

1. Health check.
2. Look up routine; if already STOPPED, exit. If state is STAGNANT or COMPLETED, neutralize the underlying task fully and transition to STOPPED.
3. If ACTIVE: neutralize per Guardrail 8, transition to STOPPED.
4. Two-step checkpoint commit `iter-NNN: stop <routine_id>`.
5. Re-render `plan.txt`. Print status.

## Mode: `start <routine_id>`

1. Health check.
2. Look up routine. If ACTIVE, exit. If STOPPED/STAGNANT/COMPLETED:
   - Reset `stats.last_useful_iter` to current iter.
   - Re-create or re-enable the underlying task (`update_scheduled_task` with the stored cron + `enabled: true`, restore the original description prefix from `[auto-routines:DELETED:...]` back to `[auto-routines:<repo_slug>] ...`).
   - Transition to ACTIVE.
3. Two-step checkpoint commit `iter-NNN: start <routine_id>`.
4. Re-render `plan.txt`. Print status.

## Mode: `revert <iter-NNN>`

1. Look up SHA in `.iteration/checkpoints.md`.
2. `git revert --no-edit --empty=drop <sha>..HEAD`. Never `--hard`.
3. Verify all `git-hook` output paths are still in `.gitignore`. (Required for clean revert.)
4. Reconcile scheduled tasks to match the now-current `config.yaml`.
5. Re-render `plan.txt`. Print status.

## Wiring routines (primitive selection)

| Trigger style                         | Primitive used                                                                                  |
| ------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Time-based (cron / hourly / daily)    | `mcp__scheduled-tasks__create_scheduled_task` — taskId `auto-routines-<repo_slug>-<routine_id>` |
| After every Claude session ends       | `Stop` hook in `.claude/settings.json`                                                          |
| After Claude runs a specific tool     | `PostToolUse` hook in `.claude/settings.json`                                                   |
| When the user submits a prompt        | `UserPromptSubmit` hook in `.claude/settings.json`                                              |
| On real git commit                    | `.git/hooks/post-commit` shell script that calls `claude -p "<prompt>"`. **Append any file the hook writes to `.gitignore`.** |
| On PR opened / CI status / new comment| Scheduled task that polls `gh pr list` + `gh run list`                                          |
| Long-running watch                    | `/loop` skill (per-routine launcher in `.claude/skills/<routine_id>/SKILL.md`)                  |

The `Stop` hook installed at init time always exists (drains evolve_requests). Per-routine `Stop` hooks coexist with it — multiple `Stop` entries in `settings.json` all fire.

## Frequency choice → cron mapping

When the user picks a frequency in the interview, store both fields:

| Choice                           | `trigger.cron`         | `trigger.human`              |
| -------------------------------- | ---------------------- | ---------------------------- |
| every 15 minutes                 | `*/15 * * * *`         | `every 15 minutes`           |
| every 30 minutes                 | `*/30 * * * *`         | `every 30 minutes`           |
| every hour                       | `0 * * * *`            | `every hour`                 |
| twice daily — 9:00 AM and 5:00 PM| `0 9,17 * * *`         | `9:00 AM and 5:00 PM daily`  |
| daily — 9:00 AM                  | `0 9 * * *`            | `9:00 AM daily`              |
| weekdays only — 5:00 PM          | `0 17 * * 1-5`         | `5:00 PM weekdays`           |
| on every git commit              | (none)                 | `on every git commit`        |
| custom                           | (parse user input)     | (echo back the user's phrase)|

## Status display (the only output format)

`.iteration/plan.txt` — regenerated after every iter, printed inline at end of `init`/`evolve`/`status`/`stop`/`start`/`revert`. Format:

```
goal:        <one-line goal>     mode: <goal-driven|fully-auto>
meta evolve: <meta.human>   ─   last fired <relative>, next <human time>

routine            schedule                state       runs  useful  noisy   notes
─────────────────  ──────────────────────  ──────────  ────  ──────  ─────   ──────────────────
<id>               <trigger.human>         <state>     <n>   <n>     <n>     <one-line note>
...

evolve requests pending: <count from .iteration/evolve_requests.jsonl>
last iter:               iter-NNN — <relative> — triggered by: <schedule|routine_id|manual>
```

Sort routines by: ACTIVE first, then EVOLVING, STAGNANT, COMPLETED, STOPPED. Within each state, alphabetical.

Notes column lifts from: most recent `iter-NNN.md` action involving this routine (e.g. `retuned at iter-008`, `paused at iter-009`, `goal: 0 vulns reached`).

## Placeholder semantics

`templates/routine-skill.md` uses `{{var}}` placeholders. Fill from:

| Placeholder                | Source                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------- |
| `{{routine_id}}`           | `routine.id`                                                                                      |
| `{{purpose}}`              | `routine.purpose`                                                                                 |
| `{{installed_at}}`         | ISO 8601 of install                                                                               |
| `{{iter_added}}`           | `routine.iter_added`                                                                              |
| `{{primitive}}`            | `routine.primitive`                                                                               |
| `{{trigger_summary}}`      | `routine.trigger.human`                                                                           |
| `{{success_criterion}}`    | `routine.success_criterion` or `(none — runs indefinitely)`                                       |
| `{{self_evolve_block}}`    | If `routine.self_evolve: true`, include the snippet that appends to `evolve_requests.jsonl` when the routine concludes its config is wrong. Otherwise omit. |
| `{{routine_specific_inputs}}` | Bullet list. PR routines get `gh pr list --state open --limit 20`; CI routines get `gh run list --limit 10`; commit routines get `git log --since="<last-fire>"`. |
| `{{routine_prompt_body}}`  | Generated from interview answer + standard footer "log outcome to `.iteration/log.jsonl`".        |

## Files this skill manages

```
.iteration/
  config.yaml              # routines registry, goal, mode, deps, neutralized_tasks
  log.jsonl                # outcomes from each routine run
  evolve_requests.jsonl    # mid-run evolve requests (drained by `evolve`)
  checkpoints.md           # iter SHAs for revert
  plan.txt                 # current text status block (rewritten every run)
  history/iter-NNN.md      # per-iteration summary
  halted.md                # written when a dep check fails (deleted on resume)
  sanity-failed-NNN.md     # written when a proposed config fails sanity
  next-goal.md             # written when a goal-driven iteration goal is met
.claude/
  settings.json            # Claude Code hooks (merged) — includes the Stop hook that drains evolve_requests
  skills/<routine_id>/     # per-routine prompt skills
.git/hooks/post-commit     # only if a routine declares primitive: git-hook
```

## Notes

- Re-read `mode` each `evolve` and respect it.
- Never silently disable a routine. Always log to `history/iter-NNN.md` why.
- Anti-flap: a routine auto-removed twice may not be re-proposed within `meta.anti_flap_window` iters.
- Logs in `log.jsonl` are append-only. Each line: `{ts, routine, outcome, summary, accepted_by_user?, increment_signal?}`. The `increment_signal` boolean (true if the routine produced something useful this run) feeds stagnation detection — set it from the routine's own prompt logic.
- Routines commit on branches `routines/<routine_id>` and open PRs — never push to main.
- Tests live in `tests/` (TDD: every check in `scripts/sanity-check.py` has a corresponding test). Run with `pytest -q`.
- All scripts in `scripts/` and templates in `templates/` — read those instead of inlining.
