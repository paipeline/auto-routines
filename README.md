# auto-routines

> **A self-evolving brain for your repo.** Set it up once. Every day it reads your commits, PRs, CI runs, and its own logs — then rewrites its own automation set to fit what your project needs *now*.

Most automation tools install once and rot. This one rewrites itself.

`auto-routines` is a [Claude Code](https://docs.claude.com/claude-code) skill. You point it at any repo, answer a one-time interview, and it installs a set of routines (scheduled tasks, Claude Code hooks, real git hooks, long-running loops, PR-comment agents). A daily meta-routine watches your repo's signals and adapts the routines — adding what's missing, demoting what's noisy, retuning frequencies — checkpointing every change so you can revert with one command.

---

## The pitch in 60 seconds

1. **You don't pick automations. The agent does.** It analyzes your stack, asks for your goal, then proposes routines that fit. You confirm once.
2. **The agent picks the right primitive automatically.** Cron, Claude hook, real `.git/hooks/post-commit`, `gh`-polling task, `/loop` — based on what each routine actually needs.
3. **It evolves.** A daily meta-routine reads `git log`, `gh pr list`, `gh run list`, and routines' own outcome logs. Adds. Removes. Retunes.
4. **You always see the plan.** A mermaid diagram is rewritten and printed inline after every run. No black box.
5. **Every iteration is a git commit.** `iter-007: ...` — revert any iteration with one command.
6. **A deterministic sanity check gates every change.** Bad cron? Reserved id? Missing dep? The validator blocks the apply before it touches your repo.

---

## How it works

```mermaid
flowchart TD
  classDef node fill:#fff,stroke:#000,color:#000;
  linkStyle default stroke:#000;

  A["/auto-routines init"]:::node --> B[analyze stack, tests, CI, git activity]:::node
  B --> C[interview: goal, mode, MCPs, candidate routines]:::node
  C --> D[render plan.mmd + sanity check]:::node
  D --> E[install: hooks, scheduled tasks, git hooks, loop launchers]:::node
  E --> F[meta-routine scheduled daily]:::node

  F --> G["/auto-routines evolve (auto, daily)"]:::node
  G --> H[gather signals: commits, PRs, CI, log.jsonl]:::node
  H --> I[decide: add / remove / retune]:::node
  I --> J[sanity check the new config]:::node
  J --> K[checkpoint commit iter-NNN]:::node
  K --> L[apply changes, neutralize orphans]:::node
  L --> M[re-render plan.mmd, write history]:::node
  M --> G
```

---

## Quick start

```bash
git clone https://github.com/paipeline/auto-routines ~/.claude/skills/auto-routines
cd /your/project
claude
> /auto-routines
```

Requirements: [Claude Code](https://docs.claude.com/claude-code), `gh` CLI, Python 3.9+ with `pyyaml`, the `scheduled-tasks` MCP enabled.

That's it. The skill interviews you, you confirm the plan, it installs.

---

## What it looks like running

```
$ /auto-routines evolve

sanity check: OK
checkpoint: iter-008  (sha 3a1f9c2)

changes:
  + added doc-drift-fixer (cron: 0 17 * * 1-5) — README diverging from src/api/
  ~ retuned pr-ci-watcher 30m → 15m — CI flake rate tripled this week
  - neutralized weekly-dep-audit — 0 useful findings in 11 runs
```

```mermaid
flowchart TD
  classDef node fill:#fff,stroke:#000,color:#000;
  linkStyle default stroke:#000;

  GOAL["Goal: ship v1.0 with great test coverage<br/>Mode: fully-auto"]:::node
  META["Meta /auto-routines evolve<br/>cron: 0 9 * * * — next: tomorrow 09:00"]:::node
  GOAL --> META

  T1["every 15 min<br/>(*/15 * * * *)"]:::node --> R1["pr-ci-watcher<br/>comment on failing PRs"]:::node
  T2["18:00 daily<br/>(0 18 * * *)"]:::node --> R2["daily-digest<br/>summary of the day"]:::node
  T3["weekdays 17:00<br/>(0 17 * * 1-5)"]:::node --> R3["doc-drift-fixer<br/>README ↔ src/api/"]:::node
  T4["git post-commit<br/>(.git/hooks/post-commit)"]:::node --> R4["test-runner-nudge<br/>nudge to run tests"]:::node

  META -.->|may tune| R1
  META -.->|may tune| R2
  META -.->|may tune| R3
  META -.->|may tune| R4
```

This block is rendered live by GitHub. The same mermaid is what your `.iteration/plan.mmd` looks like — refreshed every run.

---

## Modes

| Mode          | When you use it                                                                                                  |
| ------------- | ---------------------------------------------------------------------------------------------------------------- |
| `goal-driven` | You set an explicit iteration goal. The meta-agent picks routines that close the gap to that goal.               |
| `fully-auto`  | The meta-agent picks direction from signals alone — CI flake rate, PR queue depth, doc drift, commit cadence. The project takes care of itself. |

---

## Trigger taxonomy — the agent picks the right one

| Trigger                                | Primitive used                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------- |
| Time-based (cron / hourly / daily)     | `scheduled-tasks` MCP task                                                                  |
| After every Claude session ends        | `Stop` hook in `.claude/settings.json`                                                      |
| After Claude runs a tool               | `PostToolUse` hook                                                                          |
| When the user submits a prompt         | `UserPromptSubmit` hook                                                                     |
| **On real git commit**                 | `.git/hooks/post-commit` shell script — Claude Code has no on-commit hook event             |
| On PR opened / CI status / new comment | `gh`-polling scheduled task                                                                 |
| Long-running watch                     | `/loop` skill (per-routine launcher)                                                        |

---

## Commands

```
/auto-routines              # init if first run, else show status
/auto-routines init         # force re-interview (preserves history)
/auto-routines evolve       # run one iteration (the daily meta-routine calls this)
/auto-routines status       # show goal, active routines, current plan
/auto-routines plan         # re-render and print plan.mmd
/auto-routines revert iter-007
```

---

## Safety model

| Concern                              | Mitigation                                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Bad config applied                   | `scripts/sanity-check.py` runs before every apply; halts if invalid                                     |
| Routine flapping                     | `meta.anti_flap_window` blocks re-adding a recently-removed routine                                     |
| Broken external dep                  | Healthcheck at start of every mode; halts and writes `.iteration/halted.md`                             |
| Bad iteration                        | Every iter is its own git commit; `revert iter-NNN` restores                                            |
| Routines spamming PRs / main         | Routines commit on `routines/<id>` branches and open PRs; never push to main                            |
| Two repos colliding on task names    | Every task carries a description prefix `[auto-routines:<repo-slug>]` used as ground truth for ownership |
| Empty-commit revert pause            | `revert` uses `git revert --no-edit --empty=drop`                                                       |
| Hook-output files breaking revert    | `git-hook` install auto-appends hook-output paths to `.gitignore`                                       |

---

## What's inside

```
SKILL.md                  # the skill instructions Claude reads
README.md                 # this file
scripts/sanity-check.py   # deterministic config validator (no LLM in the loop)
templates/
  config.yaml             # routine registry schema
  plan.mmd                # mermaid scaffold
  routine-skill.md        # per-routine prompt template
  history-entry.md        # iteration history template
LICENSE                   # MIT
```

After install, the consuming repo gets:

```
.iteration/
  config.yaml             # routines, goal, mode, deps, neutralized_tasks
  log.jsonl               # outcomes from each routine fire
  checkpoints.md          # iter SHAs for revert
  plan.mmd                # current mermaid plan
  history/iter-NNN.md     # per-iteration summary
.claude/
  settings.json           # Claude Code hooks (merged, not overwritten)
  skills/<routine>/       # per-routine prompt skills
.git/hooks/post-commit    # only if a routine declares primitive: git-hook
```

---

## Validation

The skill has been tested end-to-end against a temp repo:

- ✅ `init` writes config, plan, history, hook entries, per-routine skill files
- ✅ Real `git commit` triggers the post-commit hook (logged 6 fires across the test)
- ✅ `evolve` reads signals from `log.jsonl`, decides changes, writes new config
- ✅ Sanity check gates every apply (5/5 stages exit 0)
- ✅ Each iter is its own git commit; `checkpoints.md` records SHAs
- ✅ `revert iter-001` restores prior config + plan
- ✅ Meta-routine prompt re-invokes the skill correctly with `cd <abs path>`
- ✅ MCP `create_scheduled_task` round-trip (create → list → update)

What still requires real-world runs to verify:
- Whether the meta-routine fires at 09:00 each day (real cron tick)
- Live `gh pr list` / `gh run list` integration
- Anti-flap window enforcement across many iters

---

## Known limitations

- **The `scheduled-tasks` MCP has no delete verb** (only `create`, `list`, `update`). To "remove" a routine, this skill **neutralizes** the task: sets `enabled: false`, `cronExpression: "0 0 1 1 *"`, and rewrites the description with prefix `[auto-routines:DELETED:<repo-slug>]`. Tracked in `config.yaml > neutralized_tasks`. When the MCP gains delete, the step becomes a real delete.
- **There is no Claude Code hook event for "on git commit"**. Real on-commit triggers are real `.git/hooks/post-commit` shell scripts. Claude Code hooks fire on Claude actions, not git/filesystem events.
- **The MCP sanitizes `taskId` to plain kebab-case** — slashes and underscores are stripped. The skill uses the description prefix as ground truth for ownership, not the taskId.

---

## Why this exists

Most projects start with crisp norms (run tests on every commit, audit deps weekly, write changelogs, summarize PRs) and slowly drift back to "I'll do it later." Discipline rots. `auto-routines` flips it: instead of asking *you* to maintain the routines, it lets a meta-agent maintain *itself* — and shows you the plan every time so you stay in the loop.

Goal-driven mode exists for projects with a deadline. Fully-auto mode exists for projects that should just keep themselves healthy.

---

## License

MIT — see [LICENSE](LICENSE).

If this is useful to you, star it. PRs welcome.
