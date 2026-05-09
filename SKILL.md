---
name: auto-routines
description: Analyze a repository, interview the user about goal and automation appetite, then install and evolve a set of repo-specific routines (hooks, scheduled tasks, loops, PR-comment agents, git hooks). A daily meta-routine adapts the set based on commits, PRs, CI status, and routine logs. Invoke when the user runs `/auto-routines [init|evolve|status|revert|plan]` or asks to set up / iterate project automations.
---

# auto-routines

You are operating the `auto-routines` skill. The user wants their repository to run a self-evolving set of automations. Your job is to interview them once, install routines, and from then on the daily `evolve` mode adapts the set autonomously while always showing the user the current plan as a mermaid diagram and gating risky changes behind a sanity check.

## Modes

Detect mode from the user's invocation:

- **No args, no `.iteration/` exists** → `init`
- **No args, `.iteration/` exists** → `status`
- **`init`** → run full init flow (re-running re-interviews, but preserves history)
- **`evolve`** → run the daily iteration (this is what the scheduled task calls)
- **`status`** → print current goal, mode, active routines, last iteration, and `plan.mmd`
- **`revert <iter-NNN>`** → restore checkpoint
- **`plan`** → re-render `plan.mmd` from `.iteration/config.yaml` and print it

## Guardrails (apply to every mode)

1. **Always print `plan.mmd` inline** at the end of `init`, `evolve`, `status`, `plan`. Never just point to the file.
2. **Sanity check before any apply**: run `scripts/sanity-check.py .iteration/config.yaml` (or against a proposed config). If it fails, halt and surface the report. Never apply broken config.
3. **Checkpoint before any change in `evolve`**: create a git commit `iter-NNN: <summary>` and record SHA in `.iteration/checkpoints.md`. The user must always be able to `revert` to the previous iter.
4. **Dependency health check**: at start of every mode, verify `gh auth status` (if `deps.gh != none`) and every MCP listed in `config.yaml > deps.mcps`. If any fail, write `.iteration/halted.md` with the failure and STOP. On next invocation, recheck — if healthy, delete `halted.md` and continue.
5. **Confirm before initial install**: after interview + render, show plan and ask the user to confirm. Subsequent `evolve` runs are fully auto (no confirm).
6. **Never write to `.iteration/` from inside `init` until the user confirms.** Stage proposals in `/tmp/auto-routines-staging-<repo_slug>/` until confirmed; sanity-check runs against the staged file. Anti-flap (which reads existing `.iteration/history/`) is naturally a no-op on first install.
7. **Ownership of scheduled tasks** — the `scheduled-tasks` MCP is per-user and shared across repos, and it sanitizes `taskId` to plain kebab-case (slashes/underscores are stripped). To make ownership reliable:
   - **`taskId` format**: `auto-routines-<repo_slug>-<routine_id>` (all lowercase, all hyphen-separated; this survives MCP sanitization). The meta-routine uses `auto-routines-<repo_slug>-meta`.
   - **Description is the ground truth for ownership**: every task this skill creates MUST set its `description` to start with `[auto-routines:<repo_slug>]` (e.g. `[auto-routines:my-app] PR-CI watcher — comments on failing PRs`). Filtering on `taskId` prefix is unreliable because two slugs can produce ambiguous concatenations; filter on the description prefix instead.
   - **Store the actual `task_id` per routine in `config.yaml`** under `routines[].task_id`, captured from the MCP response after `create_scheduled_task` returns. Never re-derive at use time.
   - **Before creating any task**: call `mcp__scheduled-tasks__list_scheduled_tasks` and check whether the planned `taskId` already exists. If it does, inspect its description: if it starts with `[auto-routines:<repo_slug>]`, reuse via `update_scheduled_task`; otherwise abort and surface the conflict — do NOT overwrite a task you don't own.
   - **Orphan detection on `evolve`/`revert`**: list all tasks, filter to those whose description starts with `[auto-routines:<repo_slug>]`, diff against `config.yaml > routines[].task_id` (plus the meta task). Anything in the listing not in config is an orphan — neutralize it (see Guardrail 8). Never touch tasks outside this prefix.
8. **Neutralize, don't delete (current MCP limitation).** The `scheduled-tasks` MCP exposes only `create`, `list`, `update` — no `delete` verb. To "remove" a task:
   - Call `update_scheduled_task` with `enabled: false`, `cronExpression: "0 0 1 1 *"` (Jan-1-only), and a description rewritten to start with `[auto-routines:DELETED:<repo_slug>]`. The task is then disabled and effectively never fires.
   - Record the neutralized `task_id` under `config.yaml > neutralized_tasks` so future iters know not to recreate or treat it as orphan.
   - Surface a one-line note in the iteration history: `Neutralized: <task_id> (MCP has no delete verb).`
   - This restriction is documented in [README.md](README.md). When the MCP gains a delete verb, replace this step with a real delete.
9. **Slug normalization** — `repo_slug` is computed once at `init` and stored in `config.yaml`. Never recompute later (the user might rename the directory). Normalize as follows:
   - Start with `basename "$(git rev-parse --show-toplevel)"`.
   - Lowercase.
   - Replace any character not in `[a-z0-9-]` with `-` (covers spaces, underscores, dots, unicode).
   - Collapse runs of `-` to a single `-`.
   - Strip leading/trailing `-`.
   - **Strip a leading `auto-routines-` prefix** if present, to avoid a double-prefix taskId like `auto-routines-auto-routines-foo-pr-ci-watcher`. After stripping, re-strip leading `-`. (E.g., basename `auto-routines-demo` → slug `demo`.)
   - If the result is empty or starts with a digit, prepend `r-`.
   - If the result is longer than 32 chars, truncate to 32 (we need room for the routine id in the task name).
   - Validate the final slug matches the regex `^[a-z][a-z0-9]*(-[a-z0-9]+)*$`. If not, abort and ask the user for a manual slug via `AskUserQuestion`.

## Mode: `init`

1. **Preflight**
   - Verify `git rev-parse --is-inside-work-tree` succeeds. If not, abort with a message.
   - Run `gh auth status`. If it fails, ask the user via `AskUserQuestion` whether to (a) abort, (b) install GH-dependent routines as `enabled: false` pending auth, or (c) walk them through `gh auth login` now.
   - List currently connected MCP servers (read user `~/.claude/settings.json` and project `.claude/settings.json` if present).

2. **Compute identity**
   - Compute `repo_slug` per Guardrail 9. This is needed before sanity-check (which requires it) and before any task name is constructed.

3. **Analyze**
   - Detect stack: `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile`, etc.
   - Detect tests: `tests/`, `__tests__/`, `*_test.go`, presence of test scripts.
   - Detect CI: `.github/workflows/`, `.gitlab-ci.yml`, etc.
   - Read `git log --oneline -50` for cadence.
   - Read `gh pr list --state all --limit 20` if `gh` available.
   - Read `README*` for stated purpose.

4. **Interview** (use `AskUserQuestion` for every step)
   - **Goal**: "What's the goal of this project (one sentence)?"
   - **Mode**: pick `goal-driven` (each iteration sets an explicit goal) or `fully-auto` (meta-agent picks direction from signals).
   - **MCPs to declare**: multi-select from connected MCPs the user wants routines to depend on (e.g., scheduled-tasks, twitter, linear). Selected MCPs become `deps.mcps` in config — `evolve` halts if any go offline.
   - **Candidate routines**: based on analysis, propose 4–8 candidates. Examples to consider:
     - PR-CI watcher (comment on PRs when CI fails or passes; suggest fixes)
     - Code review agent (comment on PRs with review feedback)
     - Bug detector (scheduled scan of recent commits for obvious regressions)
     - Doc drift fixer (when code changes diverge from README)
     - Test gap filler (find untested critical paths and propose tests)
     - Dep / security audit (weekly)
     - Daily progress digest (summarize commits + PRs)
     - Server / dashboard reader (poll an endpoint and react)
     - Twitter/X feedback intake (if MCP connected)
     - Architecture drift watcher (flag deviations from declared patterns)
   - For each enabled candidate, ask: trigger style (see "Wiring routines" below), frequency or cron, and a short prompt of what it should do. Defaults provided.

5. **Render & confirm**
   - Stage a candidate `config.yaml` at `/tmp/auto-routines-staging-<repo_slug>/config.yaml`.
   - Run `python3 scripts/sanity-check.py /tmp/auto-routines-staging-<repo_slug>/config.yaml`. If it fails, fix or re-ask. If it passes, render `plan.mmd` per the "Placeholder semantics" section, print the mermaid + the sanity report.
   - Ask user to confirm via `AskUserQuestion`.

6. **Install** (only after confirm)
   - Move the staged config from `/tmp/...` into `.iteration/config.yaml`. Create empty `.iteration/log.jsonl` and `.iteration/checkpoints.md`. Write `.iteration/plan.mmd` and `.iteration/history/iter-001-init.md`.
   - **Pre-flight ownership check**: call `mcp__scheduled-tasks__list_scheduled_tasks`, filter to tasks whose description starts with `[auto-routines:<repo_slug>]`. If any exist (leftover from a prior install you never cleaned up), surface them and ask via `AskUserQuestion` whether to (a) reuse if names align, (b) neutralize them all then proceed, or (c) abort.
   - For each routine, follow the "Wiring routines" matrix below to install hooks / scheduled tasks / git hooks / loop launchers. For scheduled tasks, capture the returned `taskId` from the MCP and write it back to `config.yaml > routines[<i>].task_id`.
   - For every `git-hook` routine: identify any file path the post-commit script writes to (logs, status files, etc.) and append it to the repo's `.gitignore`. If `.gitignore` doesn't exist, create it. This is required to keep `revert` working (see Mode `revert` step 3).
   - Schedule the meta-routine: `mcp__scheduled-tasks__create_scheduled_task` with `taskId = "auto-routines-<repo_slug>-meta"`, `description = "[auto-routines:<repo_slug>] meta — daily evolve"`, and prompt `cd <abs repo path> && /auto-routines evolve` (the `cd` is required — the scheduled task runs with no inherited cwd, so a bare `/auto-routines evolve` would not find `.iteration/`). Capture and store `task_id` under `config.yaml > meta.task_id`.
   - Commit the install in two steps so checkpoints.md is part of the iter commit, not a trailing dirty file:
     1. `git add .iteration .claude && git commit -m "iter-001: install auto-routines"`
     2. Capture `SHA=$(git rev-parse HEAD)`; append a line `iter-001: <SHA>  <ISO timestamp>` to `.iteration/checkpoints.md`; `git add .iteration/checkpoints.md && git commit --amend --no-edit`. The amend folds the SHA-recording into the iter commit itself so revert is consistent.
   - Print the final `plan.mmd` and a summary of installed routines and the cron of the meta-routine.
   - Delete `/tmp/auto-routines-staging-<repo_slug>/` on success.

## Mode: `evolve`

This is the daily meta-routine. Fully auto — no user confirm. But every change is checkpointed and a sanity check gates apply.

1. **Health check** (Guardrail 4). If any dep fails, write `.iteration/halted.md` and STOP.
2. **Gather signals**
   - `git log --since="<last-iter-time>"`
   - `gh pr list --state all --limit 30 --json number,state,title,statusCheckRollup`
   - Tail `.iteration/log.jsonl` (routine outcomes since last iter)
   - Read `.iteration/config.yaml`
   - Read recent CI failures via `gh run list --limit 20`
3. **Decide** (the agent's judgment, guided by `mode`)
   - In **goal-driven** mode: re-read the iteration goal, evaluate progress, propose ADD/REMOVE/EDIT routines that close the gap. If the iteration goal is met, write a new goal proposal to `.iteration/next-goal.md` and continue with current routines.
   - In **fully-auto** mode: look at signals — high CI failure rate? Add CI-fix routine. Stale README? Add doc-drift routine. Routine X has 0 useful outputs in 14 runs? Demote frequency or remove. PR queue piling up? Add review agent.
   - Anti-flap: skip any routine whose id has been removed within the last `meta.anti_flap_window` iterations (default 7).
4. **Sanity check** the proposed new config: write to `/tmp/auto-routines-staging-<repo_slug>/config.yaml`, run `python3 scripts/sanity-check.py /tmp/auto-routines-staging-<repo_slug>/config.yaml`. If fails, write `.iteration/sanity-failed-NNN.md`, halt, do NOT apply.
5. **Checkpoint** (same two-step amend pattern as `init` step 6 to keep checkpoints.md inside the iter commit):
   1. `git add -A && git commit -m "iter-NNN: <summary of changes>"`
   2. `SHA=$(git rev-parse HEAD)`; append `iter-NNN: <SHA>  <ISO timestamp>` to `.iteration/checkpoints.md`; `git add .iteration/checkpoints.md && git commit --amend --no-edit`.
6. **Apply** changes diff-based — only add/remove/edit changed routines:
   - **Add**: install per "Wiring routines"; capture `task_id` for scheduled routines.
   - **Edit**: `mcp__scheduled-tasks__update_scheduled_task` for scheduled routines (reuse the stored `task_id`); rewrite hook entries; rewrite per-routine SKILL.md.
   - **Remove**: neutralize per Guardrail 8 (no delete verb available); record under `config.yaml > neutralized_tasks`.
   - **Final reconciliation**: list all tasks, filter to `description.startswith("[auto-routines:<repo_slug>]")`. Anything not in `config.yaml > routines[].task_id` (and not the meta task) is an orphan → neutralize. Never touch tasks outside this prefix.
7. **Render** new `plan.mmd`.
8. **Log** to `.iteration/history/iter-NNN.md` and `.iteration/log.jsonl`.
9. **Print** `plan.mmd` and a one-paragraph summary of what changed and why.

## Mode: `status`

1. Read `.iteration/config.yaml` → goal, mode, routines.
2. Read last 5 entries from `.iteration/history/`.
3. Read `.iteration/log.jsonl` tail (last 20 outcomes).
4. Print `plan.mmd` inline.
5. Print a compact summary: goal, mode, N routines (active/disabled), last iter timestamp, recent notable outcomes.

## Mode: `revert <iter-NNN>`

1. Look up SHA in `.iteration/checkpoints.md`.
2. `git revert --no-edit --empty=drop <sha>..HEAD` (the `--empty=drop` is required because some iters produce no file changes — e.g. a no-op `evolve` that only rewrites `last_run` — and a plain `revert` would pause on those, breaking the chain). Never use `--hard` (we keep history).
3. **Prerequisite for clean revert**: any file written by an active `git-hook` routine MUST be in `.gitignore`. Otherwise the post-commit hook fires during the revert chain, recreates the file, and blocks subsequent reverts. The skill enforces this at install time (see "Wiring routines" below) — verify before running revert.
4. Reconcile scheduled tasks to match the now-current `config.yaml`:
   - For routines now present whose `task_id` is missing from the MCP listing → `create_scheduled_task` and recapture id.
   - For tasks under our description prefix not in current config → neutralize per Guardrail 8.
5. Re-render `plan.mmd` from the now-current `config.yaml`.
6. Print the restored plan.

## Mode: `plan`

Re-render `plan.mmd` from current `.iteration/config.yaml`, save it, print inline.

## Wiring routines (primitive selection)

The skill picks the right primitive per routine. **Claude Code hooks fire on Claude actions, not on filesystem or git events** — keep that distinction in mind when choosing.

| Trigger style                             | Primitive used                                                                                  |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Time-based (cron / hourly / daily)        | `mcp__scheduled-tasks__create_scheduled_task` — taskId `auto-routines-<repo_slug>-<routine_id>` |
| After every Claude session ends           | `Stop` hook in `.claude/settings.json`                                                          |
| After Claude runs a specific tool         | `PostToolUse` hook in `.claude/settings.json` (matches on tool name)                            |
| When the user submits a prompt            | `UserPromptSubmit` hook in `.claude/settings.json`                                              |
| On real git commit                        | `.git/hooks/post-commit` shell script that calls `claude -p "<prompt>"` non-interactively. NOT a Claude Code hook — `post-commit` is a git hook, not in `settings.json`. **Any file the hook writes (logs, status files, etc.) MUST be added to `.gitignore`** at install time, or revert breaks: a tracked hook-output file gets recreated by the hook during the revert chain and blocks subsequent reverts. The skill enforces this by appending hook-output paths to `.gitignore` whenever it installs a `git-hook` primitive. |
| On real file save (outside Claude)        | Not natively supported. Use a scheduled poll routine that diffs `git status` since last run.    |
| On PR opened / CI status / new comment    | Scheduled task that polls `gh pr list` + `gh run list` and reacts                               |
| On bug threshold / issue tracker          | Scheduled task that reads issue tracker / logs and gates on threshold                           |
| Long-running watch                        | `/loop` skill (write launcher in `.claude/skills/<routine_id>/SKILL.md`)                        |
| On goal change                            | Manual `/auto-routines plan`; goal changes are not auto-detected                                |

Routine prompts (what Claude does when fired) live in `.claude/skills/<routine_id>/SKILL.md`, generated from `templates/routine-skill.md`. The scheduled task / hook just invokes that skill.

## Placeholder semantics

`templates/routine-skill.md` and `templates/plan.mmd` use `{{var}}` placeholders. When you render them, fill from these sources:

**`templates/routine-skill.md`:**

| Placeholder                | Source                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------- |
| `{{routine_id}}`           | `routine.id`                                                                                      |
| `{{purpose}}`              | `routine.purpose`                                                                                 |
| `{{installed_at}}`         | ISO 8601 of `init` time (or iter-N apply time when added later)                                   |
| `{{iter_added}}`           | `routine.iter_added`                                                                              |
| `{{primitive}}`            | `routine.primitive` (`scheduled` / `hook` / `loop` / `pr-poll` / `git-hook`)                      |
| `{{trigger_summary}}`      | Human one-liner: e.g. `every 30 min` (cron), `Stop hook` (hook), `manual /loop` (loop), `git post-commit` (git-hook) |
| `{{routine_specific_inputs}}` | Bullet list. Always include `gh pr list --state open --limit 20` for PR routines; `gh run list --limit 10` for CI routines; `git log --since="<last-fire>"` for commit routines. Otherwise leave the bullet `(none beyond defaults)` |
| `{{routine_prompt_body}}`  | The actual instructions Claude follows when fired. Generate from the user's interview answer for that routine plus standard footer "log outcome to `.iteration/log.jsonl`". |

**`templates/plan.mmd`:**

| Placeholder           | Source                                                                                |
| --------------------- | ------------------------------------------------------------------------------------- |
| `{{goal}}`            | `config.goal`                                                                         |
| `{{mode}}`            | `config.mode`                                                                         |
| `{{meta_next_run}}`   | Output of `mcp__scheduled-tasks__list_scheduled_tasks` for the meta task → `nextRunAt` (or `today + cron` if not yet known) |

For the per-routine block, replace the example block with one block per enabled routine using this exact pattern:

```
T_<routine_id_underscored>["<trigger_summary>"]:::trigger --> R_<routine_id_underscored>["<routine_id><br/><purpose first 40 chars>"]:::routine
META -.->|may tune| R_<routine_id_underscored>
```

Where `<routine_id_underscored>` is `routine.id` with `-` → `_` (mermaid node ids cannot contain hyphens). Disabled routines get class `failed` instead of `routine`.

## The mermaid plan

`plan.mmd` shows the rest of the day:
- Top: the central agent (meta-routine) and its next scheduled run
- Middle: each active routine as a node, with its trigger annotated
- Bottom: links from triggers to actions; failed deps shown red

Always written to `.iteration/plan.mmd` AND printed inline. Use `templates/plan.mmd` as a starting structure; fill in dynamically per the table above.

## Files this skill manages

```
.iteration/
  config.yaml            # routines registry, goal, mode, deps, neutralized_tasks
  log.jsonl              # outcomes from each routine run
  checkpoints.md         # iter SHAs for revert
  plan.mmd               # current mermaid plan (rewritten every run)
  history/iter-NNN.md    # per-iteration summary
  halted.md              # written when a dep check fails (deleted on resume)
  sanity-failed-NNN.md   # written when a proposed config fails sanity
.claude/
  settings.json          # Claude Code hooks (merged, not overwritten)
  skills/<routine_id>/   # per-routine prompt skills
.git/hooks/post-commit   # only if a routine declares primitive: git-hook (calls `claude -p`)
```

## Notes

- The user is in `goal-driven` or `fully-auto` mode — re-read this each `evolve` and respect it.
- Never silently disable a routine. Always log to `history/iter-NNN.md` why.
- If a routine has been auto-removed twice, do not re-propose it within 7 iterations (anti-flap, configurable via `meta.anti_flap_window`).
- Logs in `log.jsonl` are append-only. Each line: `{ts, routine, outcome, summary, accepted_by_user?}`.
- Routines should commit on branches `routines/<routine_id>` and open PRs — never push to main.
- All scripts are in `scripts/` and templates in `templates/` — read those instead of inlining their content here.
