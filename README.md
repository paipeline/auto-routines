# auto-routines

[![CI](https://github.com/paipeline/auto-routines/actions/workflows/ci.yml/badge.svg)](https://github.com/paipeline/auto-routines/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-skill-black.svg)](https://docs.claude.com/claude-code)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-black.svg)](CONTRIBUTING.md)
[![TDD](https://img.shields.io/badge/dev-TDD-black.svg)](CONTRIBUTING.md#tdd-workflow)

> **Automation is the best harness.** A discipline agents don't maintain is a harness agents don't have. Let your repo wear the harness for you.

### 1–100 agents, working on your repo 24/7. Seconds to put to work.

```
1.  cd into any repo
2.  answer the setup questions
3.  keep the laptop / server running
4.  go get some fresh air for a few days
5.  come back to a repo that has been developing and maintaining itself
```

`auto-routines` is **not a planner** — it actually installs the harness on disk: writes `.git/hooks/post-commit` shell scripts, creates scheduled tasks via the `scheduled-tasks` MCP, generates per-routine `SKILL.md` files under `.claude/skills/`, and registers Stop hooks in `.claude/settings.json`. After install it verifies every artifact landed; if anything is missing it aborts with a written failure report. From there it runs and evolves *itself*. Each routine **writes code on a `routines/<id>` branch and opens a PR** — none of them just print findings. Pick one of two modes:

- **`fully-auto`** — the meta-agent picks direction from signals alone (CI flake rate, PR queue depth, doc drift, commit cadence). The repo keeps itself healthy.
- **`goal-driven`** — you set an iteration goal. The meta-agent picks routines that close the gap to it.

Every change the meta-agent makes is a git commit. Revert any of them with one command. You stay in the loop via a plain-text status block (am/pm times, FSM state per routine, no cron, no mermaid) refreshed after every run.

---

## Before vs. after

| | **Before auto-routines** | **After auto-routines** |
|---|---|---|
| Who runs the tests | You, when you remember | A post-commit routine, every commit |
| Who reads the CI log | You, after a Slack ping | A 15-min watcher, comments the failing line on the PR |
| Who writes the daily digest | Nobody — you "will later" | An 18:00 routine, drops it in `.iteration/digests/` |
| Who keeps the README in sync | Nobody — it rots | A weekday drift fixer, opens a PR when it diverges |
| Who removes stale automation | Nobody — it accumulates | The daily meta-agent, neutralizes routines that go quiet |
| Agents working on your repo | 0 | up to 100, on cron / hooks / loops, 24/7 |
| Your job | Hold the discipline yourself | Read the diff in the morning |

---

## Quick start

```bash
git clone https://github.com/paipeline/auto-routines ~/.claude/skills/auto-routines
cd /your/project
claude --dangerously-skip-permissions   # required: routines fire while you're away
> /auto-routines
```

Requires `gh` CLI, Python 3.9+ with `pyyaml`, and the `scheduled-tasks` MCP. The meta-routine and the scheduled tasks all invoke Claude — they only run unattended if Claude is in **auto mode** (no per-tool prompts). Without that, the harness sits there waiting for your approval on every fire.

---

## What it looks like running

```
$ /auto-routines evolve

sanity check: OK
checkpoint: iter-010 (sha 3a1f9c2) — triggered by: pr-ci-watcher

changes:
  + added release-tag-checker (every git commit) — release commits without version bump
  ~ retuned pr-ci-watcher every 30 minutes → every 15 minutes — CI flake rate tripled
  → completed weekly-dep-audit — success_criterion met (0 vulns 4 runs running)
  → stagnant doc-drift-fixer — 11 runs, 0 useful, paused (re-openable)
```

```
$ /auto-routines status

goal:        ship v1.0 with great test coverage    mode: fully-auto
meta evolve: 9:00 AM daily   ─   last fired 2h ago, next 9:00 AM tomorrow

routine            schedule                state       runs  useful  noisy   notes
─────────────────  ──────────────────────  ──────────  ────  ──────  ─────   ──────────────────────
pr-ci-watcher      every 15 minutes        ACTIVE       147     132     15   retuned at iter-010
release-checker    on every git commit     ACTIVE         7       7      0   added at iter-010
daily-digest       6:00 PM daily           ACTIVE        12      12      0
doc-drift-fixer    5:00 PM weekdays        STAGNANT      11       0      0   paused at iter-009
weekly-dep-audit   9:00 AM Mondays         COMPLETED      4       2      0   goal: 0 vulns reached

evolve requests pending: 0
last iter:               iter-010 — 2h ago — triggered by: pr-ci-watcher
```

No mermaid, no cron syntax, no black box. Times are am/pm. State is one of:

```
ACTIVE     firing on schedule
EVOLVING   meta-agent is currently re-evaluating it (transient)
STAGNANT   no incremental work for N runs — paused, re-openable
COMPLETED  success_criterion met — paused, re-openable
STOPPED    user-disabled or meta-removed — terminal (run /auto-routines start to revive)
```

---

## A use case

Three weeks into a side project, the discipline you started with has rotted. Tests skipped, README stale, CI red for two days.

You run `/auto-routines` once. The interview asks the goal, the mode, and — for each candidate routine — the **frequency** in plain English (`every 15 minutes`, `5:00 PM weekdays`, `on every git commit`), an optional **success criterion** ("CI green on last 50 PRs"), and whether the routine **may evolve itself**.

By week four the meta-agent has marked the drift fixer **STAGNANT** (no signal in 11 runs), retuned the PR watcher to every 15 minutes (CI got flaky), and **added a release-tag-checker on its own** — because it noticed you keep forgetting to bump versions. The dep-audit routine hit its **COMPLETED** state (0 vulns four runs running) and stopped firing. None of it is in your way.

Halfway through week three, the PR watcher itself dropped a line into `.iteration/evolve_requests.jsonl` saying *"CI flake rate is now 0%, recommend reducing my frequency."* The meta-agent picked it up at 9:00 AM the next morning and retuned. **Routines can ask the skill to re-evaluate them.**

You never maintained the discipline. The repo did.

---

## Commands

```
/auto-routines                                       # init if first run, else show status
/auto-routines evolve                                # run one iteration (meta calls this daily)
/auto-routines evolve --triggered-by <id> --reason <text>   # mid-run, called by routines
/auto-routines status                                # text status block (am/pm, FSM state)
/auto-routines stop <routine_id>                     # ACTIVE → STOPPED
/auto-routines start <routine_id>                    # STAGNANT|STOPPED → ACTIVE
/auto-routines revert iter-007                       # restore checkpoint
```

---

## Contributing

PRs and issues are welcome. The project is developed **test-first** — every guardrail has a failing test before the check that makes it pass. See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev loop, ground rules, and the TDD workflow.

```bash
pip install pyyaml pytest
pytest -q          # 100 tests, ~80ms
```

Good first issues are tagged [`good first issue`](https://github.com/paipeline/auto-routines/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22). Routine proposals go through the [feature-request template](.github/ISSUE_TEMPLATE/feature_request.yml).

---

## License

MIT — see [LICENSE](LICENSE). If this is useful, star it. PRs welcome.
