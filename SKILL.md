---
name: auto-routines
description: Install and evolve a finite-state set of repo-specific automations (scheduled tasks, hooks, git hooks, loops). After a one-time interview, a daily meta-routine adapts the set from signals (commits, PRs, CI, routine logs); routines themselves can request mid-run evolution. Invoke on `/auto-routines [init|evolve|status|stop|start|revert]` or when the user asks to set up project automations.
---

# auto-routines

> **TL;DR for the operator (you).** This skill installs real artifacts on disk and verifies them before declaring success. It does **not** stop at planning. Routines, when they fire, write code and open PRs. In goal-driven mode, one of the routines (`prd-implement`) drives the PRD forward on a schedule — without it the install is reactive-only.

You are operating the `auto-routines` skill. The user wants their repository to run a self-evolving set of automations. Interview them once with explicit per-routine frequency choices, install routines as a finite-state machine, and from then on the daily `evolve` mode plus mid-run evolve requests adapt the set autonomously while always showing a plain-text status block (am/pm times, no mermaid) and gating risky changes behind a sanity check.

## Two failure modes this skill exists to prevent

1. **"Here's the plan, looks good?"** — `init` must produce a `.git/hooks/post-commit` shell script, entries in `.claude/settings.json`, scheduled-task IDs from the MCP, and per-routine SKILL.md files (filled from `templates/routine-catalog.yaml`). Step 7 verifies every artifact on disk; if any is missing it aborts with `.iteration/install-failed.md`.
2. **"Reactive maintenance only."** — every routine, when fired, branches on `routines/<id>`, commits, pushes, opens a PR. The catalog's `prd-implement` archetype pushes the PRD forward one slice per fire (read `.iteration/goal.md`, plan ahead, write code + tests, PR). In `goal-driven` mode it's always proposed.

## Files this skill manages

Schema 4 (PRD #10) added the GHA execution surface and a runtime ledger.
Both are part of the install footprint now.

```
.iteration/
  config.yaml              # routines registry, goal, mode, deps, neutralized_tasks (schema_version: 4)
  state.json               # runtime ledger — orchestrator's persistent state (schema_version: 1)
  log.jsonl                # outcomes from each routine run (one line per fire)
  evolve_requests.jsonl    # mid-run evolve requests (drained by `evolve`)
  checkpoints.md           # iter SHAs for revert
  plan.txt                 # current text status block (rewritten every run)
  history/iter-NNN.md      # per-iteration summary
  halted.md                # written when a dep check fails (deleted on resume)
  sanity-failed-NNN.md     # written when a proposed config fails sanity
  next-goal.md             # written when a goal-driven iteration goal is met
.claude/
  settings.json            # Claude Code hooks (merged) — includes the always-on Stop hook that drains evolve_requests
  skills/<routine_id>/     # per-routine prompt skills (filled from templates/routine-skill.md)
  skills/_shared/preamble.md  # PRD #10 Module 3 — shared FSM/log/PR/failure rules every routine references
.github/
  workflows/auto-routines.yml  # PRD #10 Module 4 — the always-on execution surface (cron + dispatch)
.git/hooks/post-commit     # only if a routine declares primitive: git-hook
```

## Modes

Detect mode from the user's invocation:

| Invocation                                 | Mode    | What happens                                                              |
| ------------------------------------------ | ------- | ------------------------------------------------------------------------- |
| No args, no `.iteration/`                  | `init`  | Run full install flow (interview → render → confirm → install → verify).  |
| No args, `.iteration/` exists              | `status`| Print the text status block.                                              |
| `init`                                     | `init`  | Force re-interview (preserves history, re-installs).                      |
| `evolve [--triggered-by <id> --reason …]`  | `evolve`| Run one meta iteration. Optional flags record the trigger.                |
| `status`                                   | `status`| Print goal, mode, routines table (am/pm + FSM state), pending requests.   |
| `stop <routine_id>`                        | `stop`  | Transition `ACTIVE → STOPPED`. Neutralize the underlying task.            |
| `start <routine_id>`                       | `start` | Transition `STAGNANT|COMPLETED|STOPPED → ACTIVE`. Re-arm the task.        |
| `revert <iter-NNN>`                        | `revert`| `git revert` back to the named checkpoint. Reconcile tasks afterwards.    |

## Guardrails (apply to every mode)

These are the ground rules. Every mode below assumes them.

1. **Always print the text status block inline.** End every `init` / `evolve` / `status` / `stop` / `start` with the block. No mermaid. Times render as am/pm (`9:00 AM daily`, `5:00 PM weekdays`). Cron stays internal — humans see it only if they explicitly ask.

2. **Sanity-check before apply.** Run `scripts/sanity-check.py <config>` against the proposed config. On fail: halt, surface the report. Never apply broken config.

3. **Checkpoint before any change.** `evolve` / `stop` / `start` each create a `iter-NNN: <summary>` commit and append the SHA to `.iteration/checkpoints.md`. The user must always be able to `revert`.

4. **Dependency health check.** At the start of every mode, verify `gh auth status` (when `deps.gh != none`) and every MCP in `config.yaml > deps.mcps`. On fail: write `.iteration/halted.md`, STOP. Next invocation rechecks — on green, delete `halted.md` and continue.

5. **Confirm before initial install.** After interview + render, show the proposed status block and ask the user to confirm. Subsequent `evolve` runs are fully auto.

6. **Never write to `.iteration/` until the user confirms.** Stage proposals in `/tmp/auto-routines-staging-<repo_slug>/`. The sanity check runs against the staged file.

7. **Ownership of scheduled tasks.** The `scheduled-tasks` MCP is per-user, shared across repos, and sanitizes `taskId` to kebab-case. To make ownership reliable:
   - **`taskId` format**: `auto-routines-<repo_slug>-<routine_id>` (lowercase, hyphen-separated). Meta is `auto-routines-<repo_slug>-meta`.
   - **Description is ground truth.** Every task we create has `description` starting with `[auto-routines:<repo_slug>]`.
   - **Store `task_id` per routine in `config.yaml`** (`routines[].task_id`), captured from the MCP response. Never re-derive at use time.
   - **Pre-create check.** List existing tasks before `create`. If the planned `taskId` already exists and is ours: reuse via `update_scheduled_task`. Else abort.
   - **Orphan sweep.** List all tasks, filter to our prefix, diff against `config.yaml`. Anything in the listing not in config is an orphan — neutralize.

8. **Neutralize, don't delete (MCP has no `delete`).** The MCP exposes `create`, `list`, `update` only. To "remove": `update_scheduled_task` with `enabled: false`, `cronExpression: "0 0 1 1 *"` (Jan-1-only), description rewritten to start with `[auto-routines:DELETED:<repo_slug>]`. Record under `config.yaml > neutralized_tasks`.

9. **Slug normalization (once at init, stored).** `basename` → lowercase → strip non `[a-z0-9-]` → collapse `-` runs → strip leading/trailing `-` → strip leading `auto-routines-` → if empty or digit-leading prepend `r-` → truncate to 32 → validate.

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

> **Install is mandatory.** This skill does not stop at planning. Every numbered step below MUST run to completion before printing the final status block. Step 7 ("Verify") aborts if any artifact is missing on disk or in the MCP listing.

The eight steps below cluster into four phases:

| Phase           | Steps | Purpose                                              |
| --------------- | ----- | ---------------------------------------------------- |
| **Preflight**   | 1–3   | Detect repo, identity, stack.                        |
| **Interview**   | 4–5   | Ask the user; render + confirm.                      |
| **Install**     | 6     | Write artifacts on disk + MCP.                       |
| **Verify+ship** | 7–8   | Audit every artifact; print final status.            |

---

### Preflight (steps 1–3)

1. **Preflight** — verify `git rev-parse --is-inside-work-tree`; check `gh auth status`; list connected MCPs.

2. **Compute identity** — `repo_slug` per Guardrail 9.

3. **Analyze** — detect stack (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile`, …); detect tests/CI; read `git log --oneline -50`; `gh pr list --state all --limit 20` if available; read `README*`.

---

### Interview (steps 4–5)

4. **Ask the user** (use `AskUserQuestion` for every step):
   - **Goal**: "What's the goal of this project (one sentence)?"
   - **Mode**: pick `goal-driven` or `fully-auto`.
   - **MCPs**: multi-select from connected MCPs.
   - **Goal capture (goal-driven mode only)**: if mode is `goal-driven`, ask the user where the PRD/roadmap lives. If they don't have one yet, offer to write `.iteration/goal.md` from their goal sentence (single bullet list of 5–10 tasks). The `prd-implement` archetype reads this file every fire — without it the skill cannot drive feature work, only react to commits.
   - **Budget**: ask "What's your token budget for this repo?" with these options. Store as `meta.budget`. The budget pre-fills the per-routine frequency in the next step — the user can still override per-routine.
     ```
     [low]      ~5 Claude sessions/week — prd-implement weekdays 9 AM, meta weekly,
                drop daily-digest and session-doc-drift
     [medium]   ~15 sessions/week — prd-implement every 12h, meta daily,
                digest daily (script-only fallback if available),
                session-doc-drift weekly
     [high]     ~50 sessions/week — current "every 4h" cadence on prd-implement,
                meta daily, digest daily, drift weekday evenings
     [custom]   ask each routine individually (current default flow)
     ```
     The `low/medium/high → cadence` mapping is in "Budget → cadence presets" below. Apply it as the default frequency for every routine the user selects in the next step.
   - **Candidate routines**: read `templates/routine-catalog.yaml`. Match each archetype's `stack_hints` against the analysis from step 3 and propose 4–8 candidates (the matched archetypes plus 1–2 obvious gap-fillers). Show the user the archetype `id` + `purpose` and let them multi-select which to install. **In `goal-driven` mode you MUST always include `prd-implement` in the candidate list, regardless of stack hints — that archetype is the one that pushes the PRD forward on a schedule. Without it the install is just reactive maintenance.** When `meta.budget` is `low`, automatically deselect `daily-digest` and `session-doc-drift` from the candidate list (they bring marginal value at low budget) — but still show them so the user can re-add manually.
   - **For each selected candidate, ask three questions in order:**
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
        Default to the archetype's `trigger_default`. Store `trigger.cron` (canonical) AND `trigger.human`.
     2. **Success criterion** (free text, optional). Default to the archetype's `success_criterion`.
     3. **Self-evolve allowed?** (yes/no). Default to the archetype's `self_evolve` flag.
     4. **Execution surface** (`gha` / `local`) — only ask when `primitive` is `scheduled` or `pr-poll`:
        ```
        [gha]    Run on GitHub Actions — fires even when your laptop is closed (default)
        [local]  Run on your machine — needs Claude Code open + scheduled-tasks MCP
        ```
        Default `gha`. Store as `routines[].execution_surface`. (PRD #10 user story 12.)
     5. **Estimated minutes per fire** (1–30) — default 5. Used for the `gha_minutes_cap` budget tracker. Only asked when surface is `gha`.

   - **Schema-4 dials** (asked once, after the per-routine loop, stored under `meta`):
     - **Idle window** — "Should heavy autonomous work pause during your active hours? Format `HH:MM-HH:MM` (24h)."
       Options: `[23:00-07:00]` (default), `[22:00-08:00]`, `[always]` (no gating), `[custom]`. Stored as `meta.idle_window`.
     - **Idle window timezone** — `meta.idle_window_tz` is an IANA tz string (e.g. `America/Los_Angeles`, `Europe/Berlin`). Detect from the user's machine via `python -c "import datetime; print(datetime.datetime.now().astimezone().tzinfo.key)"` and confirm. Refuse silent UTC fallback — ask explicitly if detection fails.
     - **GHA cost cap** — "How many GitHub Actions minutes per day max? (Free plans: 2000 min/month ≈ 60/day)" Default `60`. Stored as `meta.gha_minutes_cap`. Self-tracked in `state.json`, reset at midnight `idle_window_tz`.

5. **Render & confirm** — stage `config.yaml` at `/tmp/auto-routines-staging-<repo_slug>/config.yaml` with `schema_version: 4`. Run `python3 scripts/sanity-check.py /tmp/auto-routines-staging-<repo_slug>/config.yaml` — this validates the new schema-4 fields (`execution_surface`, `idle_window`, `idle_window_tz`, `gha_minutes_cap`, `est_minutes`). Render the text status block per "Status display". Ask the user to confirm via `AskUserQuestion`. Do NOT proceed to step 6 without explicit confirmation.

---

### Install (step 6 — every sub-step is mandatory)

6. **Install** — perform every sub-step in order. Write the artifacts described, do not just describe them.

   **6a. Create `.iteration/` skeleton** (relative to repo root):
   ```
   .iteration/config.yaml             # move from staging
   .iteration/log.jsonl               # touch (empty)
   .iteration/evolve_requests.jsonl   # touch (empty)
   .iteration/checkpoints.md          # write header "# auto-routines checkpoints\n"
   .iteration/plan.txt                # rendered status block
   .iteration/history/iter-001-init.md
   ```

   **6b. Pre-flight ownership check** (Guardrail 7) — list scheduled tasks, filter to `[auto-routines:<repo_slug>]`. If any leftover, ask via `AskUserQuestion` whether to reuse / neutralize / abort.

   **6c. Per-routine install** — for each routine in `config.yaml > routines[]`, dispatch on `routine.primitive`:

   - **`primitive: scheduled` or `primitive: pr-poll`:**
     1. Read `templates/routine-skill.md`. Fill all `{{placeholders}}` (see "Placeholder semantics"). Use the archetype's `prompt_body` from the catalog as `{{routine_prompt_body}}` unless the user typed a custom prompt.
     2. Write the filled file to `.claude/skills/<routine_id>/SKILL.md`.
     3. Call `mcp__scheduled-tasks__create_scheduled_task` with:
        ```
        taskId: "auto-routines-<repo_slug>-<routine_id>"
        cronExpression: "<routine.trigger.cron>"
        prompt: "cd <abs repo root> && /<routine_id>"
        description: "[auto-routines:<repo_slug>] <routine.purpose>"
        enabled: true
        ```
     4. Capture the returned `taskId` and write it back to `config.yaml > routines[<i>].task_id`.
     5. Transition `state: PROPOSED → ACTIVE` in config.yaml.

   - **`primitive: hook`** (Claude Code hook event like `Stop`, `PostToolUse`, `UserPromptSubmit`):
     1. Render the per-routine SKILL.md as above.
     2. Read `.claude/settings.json` (create with `{}` if missing). Merge — never overwrite — the new hook entry under `hooks.<EventName>[]`. Each entry is:
        ```json
        {
          "matcher": "<tool-pattern or empty>",
          "hooks": [{"type": "command", "command": "claude --dangerously-skip-permissions -p '/<routine_id>'"}]
        }
        ```
     3. Write back. Validate the JSON parses before saving.
     4. Transition `state: PROPOSED → ACTIVE`.

   - **`primitive: git-hook`** (real git on-commit):
     1. Render the per-routine SKILL.md as above.
     2. Read `templates/post-commit-hook.sh`. For each git-hook routine, append to the dispatch block:
        ```bash
        ( claude --dangerously-skip-permissions -p "/<routine_id>" \
            >> "$HOOK_LOG" 2>&1 \
            && echo "{\"ts\":\"$(date +%Y-%m-%dT%H:%M:%S%z)\",\"routine\":\"<routine_id>\",\"outcome\":\"ok\"}" >> "$LOG" \
          ) &
        ```
     3. Write the assembled file to `.git/hooks/post-commit`. `chmod +x .git/hooks/post-commit`.
     4. Append `.iteration/hook-output.log` to `.gitignore` (and any other path the script writes). Required for revert (Guardrail 4 of Mode `revert`).
     5. Transition `state: PROPOSED → ACTIVE`.

   - **`primitive: loop`:**
     1. Render the per-routine SKILL.md. The launcher SKILL describes how the user starts the loop (`/<routine_id>` with `/loop` flag).
     2. Loops are user-initiated; no scheduled task. Transition `state: PROPOSED → ACTIVE`.

   **6d. Install the always-on `Stop` hook for evolve-request draining.** This is independent of any user-selected routine. Add to `.claude/settings.json > hooks.Stop[]`:
   ```json
   {
     "matcher": "",
     "hooks": [{"type": "command", "command": "test -s .iteration/evolve_requests.jsonl && claude --dangerously-skip-permissions -p '/auto-routines evolve' || true"}]
   }
   ```

   **6e. Schedule the meta-routine:**
   ```
   taskId:         "auto-routines-<repo_slug>-meta"
   cronExpression: "<config.meta.cron>"          # default 0 9 * * *
   prompt:         "cd <abs repo root> && /auto-routines evolve"
   description:    "[auto-routines:<repo_slug>] meta — daily evolve"
   enabled:        true
   ```
   Capture `task_id` to `config.yaml > meta.task_id`.

   **6f. Render the shared preamble (PRD #10 Module 3).** Render `templates/routine-preamble.md` to `.claude/skills/_shared/preamble.md`. Idempotent — safe to re-run on `evolve`. Every per-routine SKILL.md references this file via the `## Reference` pointer, so it MUST exist before the per-routine renders in 6c are useful.

   **6g. Write the GHA workflow (PRD #10 Module 4 — the execution surface).** Hard-fail if the repo isn't a GitHub repo (no `.git/config` remote pointing at github.com). Then:
   1. Read `templates/auto-routines-workflow.yml` (or render from the canonical version at `.github/workflows/auto-routines.yml` in this repo).
   2. Write to `.github/workflows/auto-routines.yml` in the user's repo.
   3. Verify the `ANTHROPIC_API_KEY` repo secret is set: `gh secret list --repo <owner/name>` and grep for it. If missing, halt with `.iteration/install-failed.md` containing the setup command:
      ```
      gh secret set ANTHROPIC_API_KEY --repo <owner/name> < /path/to/key
      ```
      Do not continue — the workflow's headless Claude step will fail every tick without the secret.

   **6h. Initialize `.iteration/state.json`.** This is the runtime ledger the orchestrator reads on every tick. Use `scripts/state.py`'s `initial_state(reset_date)` helper:
   ```python
   from scripts.state import initial_state
   today = datetime.now(ZoneInfo(meta.idle_window_tz)).date().isoformat()
   state_json = initial_state(today)
   ```
   Write it to `.iteration/state.json` (pretty-printed). The schema is pinned at `schema_version: 1`. Do NOT hand-craft this dict — the helper guarantees it passes `validate_state()`.

   **6i. Open the iter-001 dashboard issue (PRD #10 Module 2 / user story 18).**
   1. Render the initial dashboard body via `python scripts/dashboard.py sync --config .iteration/config.yaml --state .iteration/state.json --log .iteration/log.jsonl --repo <owner/name> --iter 1`.
   2. The CLI returns `{action: "created", issue_number: N, ...}` on stdout. The issue number is recorded in `state.json` automatically by the next sync; no manual write needed here.
   3. Confirm the issue is visible: `gh issue view N --repo <owner/name>`.

   **6j. Two-step commit (preserves `checkpoints.md` inside the iter commit):**
   1. `git add .iteration .claude .gitignore .git/hooks/post-commit .github/workflows/auto-routines.yml 2>/dev/null; git commit -m "iter-001: install auto-routines"`
   2. `SHA=$(git rev-parse HEAD); printf 'iter-001: %s  %s\n' "$SHA" "$(date +%Y-%m-%dT%H:%M:%S%z)" >> .iteration/checkpoints.md; git add .iteration/checkpoints.md && git commit --amend --no-edit`
   3. `git push origin HEAD` — the GHA workflow can't tick on a branch GitHub doesn't have yet.

---

### Verify + ship (steps 7–8)

7. **Verify install** — this step is what catches "I rendered a plan but installed nothing". For every routine in `config.yaml`:
   - `state: ACTIVE` in config.yaml
   - `.claude/skills/<routine_id>/SKILL.md` exists, has no unfilled `{{placeholders}}`
   - If `primitive: scheduled` or `pr-poll`: `routine.execution_surface` is set to `gha` or `local` (schema 4 requirement; sanity-check would catch this on next evolve, but we want to fail fast at install)
   - If `primitive: scheduled`: the `task_id` in config matches a real entry in `mcp__scheduled-tasks__list_scheduled_tasks` whose description starts with `[auto-routines:<repo_slug>]` (only required when `execution_surface: local`; for `gha` routines the GHA workflow is the dispatcher)
   - If `primitive: hook`: an entry exists in `.claude/settings.json > hooks.<event>[]` with command containing `/<routine_id>`
   - If `primitive: git-hook`: `.git/hooks/post-commit` is executable AND contains `/<routine_id>`
   - The meta task exists in the MCP listing
   - The always-on `Stop` evolve-drain hook exists in `.claude/settings.json`

   Schema-4 install artifacts (PRD #10):
   - `.iteration/state.json` exists, parses, and `state.validate_state()` returns `[]`
   - `.github/workflows/auto-routines.yml` exists and parses as YAML
   - `.claude/skills/_shared/preamble.md` exists and contains no unfilled `{{placeholders}}`
   - `ANTHROPIC_API_KEY` is listed in `gh secret list --repo <owner/name>`
   - `config.yaml > schema_version` is `4` (older installs must be migrated via `scripts/migrate.py` first)

   If ANY check fails, write `.iteration/install-failed.md` with the missing artifacts and abort with a non-zero exit. Do not print "install successful" if anything is missing.

8. **Print** the final status block, plus a one-line summary like:
   ```
   installed: 4 routines (3 active, 1 git-hook), meta scheduled 9:00 AM daily
   ```
   Then delete `/tmp/auto-routines-staging-<repo_slug>/`.

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
   - **When proposing a NEW routine**: pick from `templates/routine-catalog.yaml` first — its archetypes have battle-tested prompt bodies that produce real diffs. Only invent a custom routine if no archetype fits.
   - Anti-flap: skip any routine id removed within last `meta.anti_flap_window` iters.
6. **Sanity check** the proposed new config (write to `/tmp/...`, run `scripts/sanity-check.py`). On failure, write `.iteration/sanity-failed-NNN.md`, halt.
7. **Checkpoint** (two-step amend pattern as in `init`).
8. **Apply** diff-based — execute every change concretely; never just describe:
   - **Add**: run the full per-primitive install procedure from `init` step 6c (write the per-routine SKILL.md from the catalog, create the scheduled task / hook entry / git-hook block). Capture `task_id`. `state: PROPOSED → ACTIVE`.
   - **Edit / Retune**: `update_scheduled_task` (reuse stored `task_id`); rewrite the per-routine SKILL.md if its prompt changed; update `trigger.human`.
   - **Remove**: neutralize per Guardrail 8; `state: → STOPPED`.
   - **Final reconciliation**: orphan sweep (Guardrail 7).
   - **Run the same `init` step 7 verification on changed routines.** Abort the iter (and revert via the just-recorded checkpoint) if any post-apply check fails.
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

**This mode does not spawn an LLM.** Run the local script and print its output verbatim:

```bash
python3 scripts/status.py            # full table
python3 scripts/status.py --routine <id>   # one-routine drill-in
python3 scripts/status.py --json     # machine-readable
```

The script reads `.iteration/config.yaml` + `.iteration/log.jsonl` + `.iteration/evolve_requests.jsonl` and renders the same status block format documented under "Status display". No file analysis, no synthesis, no Claude tokens.

Install copies `scripts/status.py` from the skill directory into the consumer repo (step 6a). Tests in `tests/test_status.py` pin the no-LLM contract: any change that introduces `subprocess`, network calls, or LLM invocation breaks the test suite.

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

## Budget → cadence presets

`meta.budget` controls how often LLM-spawning routines fire. Set during the
interview (step 4), overridable per-routine. Tokens scale roughly linearly with
"Claude sessions per week", so this is the single biggest knob the user has.

| Routine             | low                     | medium (default)        | high                    |
| ------------------- | ----------------------- | ----------------------- | ----------------------- |
| `prd-implement`     | weekdays 9 AM           | every 12h               | every 4h                |
| `meta` (evolve)     | weekly Mon 9 AM         | daily 9 AM              | daily 9 AM              |
| `daily-digest`      | (skipped)               | daily 6 PM              | daily 6 PM              |
| `session-doc-drift` | (skipped)               | weekly Mon 5 PM         | weekday 5 PM            |
| `commit-tests`      | on commit (pure shell)  | on commit (pure shell)  | on commit (pure shell)  |
| `commit-lint`       | on commit (pure shell)  | on commit (pure shell)  | on commit (pure shell)  |

Cron strings for each preset:

| budget × routine               | cron              | human                  |
| ------------------------------ | ----------------- | ---------------------- |
| `low / prd-implement`          | `0 9 * * 1-5`     | weekdays 9:00 AM       |
| `low / meta`                   | `0 9 * * 1`       | Mondays 9:00 AM        |
| `medium / prd-implement`       | `0 */12 * * *`    | every 12 hours         |
| `medium / meta`                | `0 9 * * *`       | 9:00 AM daily          |
| `medium / daily-digest`        | `0 18 * * *`      | 6:00 PM daily          |
| `medium / session-doc-drift`   | `0 17 * * 1`      | Mondays 5:00 PM        |
| `high / prd-implement`         | `0 */4 * * *`     | every 4 hours          |
| `high / session-doc-drift`     | `0 17 * * 1-5`    | 5:00 PM weekdays       |

Token-frugal principles applied at every budget tier:
- **Status is never an LLM call.** `Mode: status` runs `scripts/status.py`.
- **Post-commit hooks are pure shell.** `commit-tests` and `commit-lint` never
  spawn Claude — they run `pytest`/`ruff` and log outcomes.
- **`daily-digest` MAY be downgraded to a pure-shell variant.** The catalog
  ships an LLM version, but for `low`/`medium` the install can swap it for a
  `git log` + `gh pr list` shell summary written into
  `.iteration/digests/`. (TODO: see `.iteration/goal.md`.)

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

## The routine catalog

`templates/routine-catalog.yaml` is the source of pre-built archetypes. Each archetype has:

- `id`, `purpose`, `primitive`, `trigger_default`, `automation_default`, `self_evolve`
- `success_criterion` template
- `stack_hints` — list of detection signals (file names, package managers, frameworks)
- `prompt_body` — concrete instructions that tell the routine to write code, commit on `routines/<id>`, and open a PR

When proposing routines (in `init` interview or `evolve` decisions), prefer archetypes whose `stack_hints` match what step 3 ("Analyze") detected. Only invent a custom routine when no archetype fits.

The catalog is treated as authoritative for `routine_prompt_body` unless the user types a custom prompt during the interview. This is what makes the skill "actually do work" rather than "render a plan."

## Notes

- Re-read `mode` each `evolve` and respect it.
- Never silently disable a routine. Always log to `history/iter-NNN.md` why.
- Anti-flap: a routine auto-removed twice may not be re-proposed within `meta.anti_flap_window` iters.
- Logs in `log.jsonl` are append-only. Each line: `{ts, routine, outcome, summary, accepted_by_user?, increment_signal?}`. The `increment_signal` boolean (true if the routine produced something useful this run) feeds stagnation detection — set it from the routine's own prompt logic.
- **All timestamps are local time with offset, never UTC.** Generate with `date +%Y-%m-%dT%H:%M:%S%z`. The `scheduled-tasks` MCP evaluates cron in the user's local timezone, so writing logs in UTC creates a confusing mismatch ("scheduled at 9 AM, log says 16:00Z, was that 9 AM or noon?"). Use local time everywhere we write to disk: `log.jsonl`, `evolve_requests.jsonl`, `checkpoints.md`, hook output. The only exception is the GitHub API — `gh` queries that take `updated:>` filters need UTC and the catalog uses `date -u` there explicitly.
- Routines commit on branches `routines/<routine_id>` and open PRs — never push to main.
- Tests live in `tests/` (TDD: every check in `scripts/sanity-check.py` has a corresponding test). Run with `pytest -q`.
- All scripts in `scripts/` and templates in `templates/` — read those instead of inlining.
