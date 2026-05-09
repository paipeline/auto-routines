# iter-001 — install (self-hosted)

When: 2026-05-09T19:26:17+0200
Branch: setup/self-hosting
Mode: goal-driven

## What landed
- `.iteration/goal.md` — PRD for evolving the skill (coverage, catalog, UX, docs)
- `.iteration/config.yaml` — schema-v3, 5 routines, anti_flap_window=3
- `.claude/skills/<routine>/SKILL.md` — 5 per-routine skills rendered from the catalog
- `.git/hooks/post-commit` — pure shell, runs pytest + ruff in background, logs to log.jsonl
- `scripts/render-routine-skills.py` — one-shot renderer used during install

## Routines installed
| id | primitive | trigger | self_evolve |
|---|---|---|---|
| prd-implement | scheduled | every 4 hours | yes |
| commit-tests | git-hook | post-commit | no |
| commit-lint | git-hook | post-commit | no |
| session-doc-drift | scheduled | 5:00 PM weekdays | yes |
| daily-digest | scheduled | 6:00 PM daily | no |

## Local-first design notes
- No Stop hook in `.claude/settings.json` — permission policy blocks
  `claude --dangerously-skip-permissions` self-spawning.
- Post-commit hook is pure shell. It never invokes Claude.
- Scheduled tasks use the `scheduled-tasks` MCP, which runs locally on this
  machine (storage at `~/.claude/scheduled-tasks/`). Cron is local time.
- All timestamps are local with offset (e.g. `+0200`), never UTC `Z`.

## Definition of done for iter-001
- [x] PRD captured
- [x] Config valid against schema-v3 sanity-check
- [x] All 5 SKILLs rendered with no `{{placeholders}}`
- [x] post-commit hook executable
- [x] .iteration scaffolding committed (config + goal + history; logs gitignored)
- [ ] MCP scheduled tasks created (next step)
- [ ] First prd-implement fire opens a PR within 4 hours
