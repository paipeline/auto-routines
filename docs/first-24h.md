# Your first 24 hours with `auto-routines`

This is the hour-by-hour map of what to expect after `/auto-routines`
finishes installing. If any step doesn't look like the example output
here, jump straight to [troubleshooting](./troubleshooting.md).

The walkthrough assumes a fresh install on a real GitHub repo with the
default `medium` budget.

---

## T+0 — Install just finished

The skill streamed a wall of `→ [6a]`, `→ [6b]`, … phase headers,
then a green welcome block. Your repo now has:

```bash
ls .iteration/
# config.yaml      → schema-4 ledger: routines, cadences, budget, state
# log.jsonl        → one line per routine fire (append-only)
# goal.md          → only present if prd-implement is installed
# tasks.md         → cached task breakdown (prd-implement, regenerated)

ls .claude/skills/
# <routine-id>/SKILL.md per installed routine — these are what the
# scheduled tick or post-commit hook actually invokes.

ls .git/hooks/post-commit
# Present + executable if any reactive routines are installed.

ls .github/workflows/auto-routines.yml
# Present if any `primitive: scheduled` routine is installed.
```

Quick health check — every check should print green:

```bash
/auto-routines status
```

Expected output (lightly redacted):

```text
auto-routines · 3 routines installed · budget=medium
  prd-implement      scheduled  every 12 hours   automation=auto   last fire: never
  daily-digest       scheduled  daily at 09:00   automation=auto   last fire: never
  session-recap      hook       on session end   automation=auto   last fire: never
state: HEALTHY · no orphaned MCP tasks · no install-failed.md
```

If `state:` is anything but `HEALTHY` or the routine list is empty,
the install didn't finish cleanly — see
[troubleshooting](./troubleshooting.md).

The welcome block also told you when to expect the first auto-PR:

```text
Your first auto-PR (from `prd-implement`) will land at: every 12 hours.
```

That line comes from `scripts/orchestrator.py first-pr-eta` — it's
not a guess; it's read straight from your config.

---

## T+1 hour — Reactive routines have fired

The first thing that runs is whatever you wired to the post-commit
hook (typically `session-recap` or a short maintenance routine).
Make a commit on any branch:

```bash
git commit --allow-empty -m "test: trigger post-commit hook"
tail -n 5 .iteration/log.jsonl
```

You should see one new line per reactive routine that fired:

```jsonl
{"ts":"2026-05-11T14:02:11Z","routine":"session-recap","outcome":"ok","summary":"3 files touched · no PR opened","duration_ms":4120}
```

The `outcome` field is your single-line health signal:

- `ok` — routine fired, did its thing, exited clean
- `noop` — routine fired but found nothing to do (also healthy)
- `err` — routine hit an unrecoverable condition and bailed; read
  `summary` for the reason

If you see no new lines at all, the post-commit hook didn't fire.
`ls -l .git/hooks/post-commit` — must be executable (`-rwxr-xr-x`).

---

## T+few hours — First scheduled tick

The GitHub Actions workflow `auto-routines.yml` fires on the schedule
your config picked. To watch it land:

```bash
gh run list --workflow=auto-routines.yml --limit 5
gh run watch                    # follow the most recent
```

A successful tick ends with a new line in `.iteration/log.jsonl` and
(for `automation=auto` routines) a new branch under `routines/`.

---

## T+~12 hours — First auto-PR lands

This is the moment the README's "1–100 agents working on your repo"
claim becomes literally true. Find it:

```bash
gh pr list --search "head:routines/" --state open
```

Expected:

```text
#42  routines/prd-implement  feat: add coverage-watcher archetype  OPEN
```

Open it, read the diff, merge or close like any other PR. The next
scheduled tick reads merge state via `gh pr list --state all` and
won't re-implement the same slice.

If after 24 hours you still see zero PRs under `routines/`:

```bash
gh run list --workflow=auto-routines.yml --limit 20 --json conclusion,createdAt
```

Look for `"conclusion":"failure"` — almost always either
`ANTHROPIC_API_KEY` missing or the routine itself hit an `err`
outcome (visible in the run log AND in `.iteration/log.jsonl`).

---

## T+~24h — Time to evolve

After watching a routine for a day you'll want to change something —
cadence, automation level, prompt body, or you want to add a routine
the interview didn't surface. That's what `/auto-routines evolve` is
for:

```bash
/auto-routines evolve
```

Evolve drains `.iteration/evolve_requests.jsonl` (where ad-hoc
requests collect between runs), proposes a config diff, writes a
checkpoint, and applies it atomically. If anything goes wrong,
re-run — the checkpoint lets it resume from where it stopped.

You can also append a request manually without a Claude session:

```bash
echo '{"ts":"2026-05-11T18:00Z","ask":"bump prd-implement to every 6h"}' \
    >> .iteration/evolve_requests.jsonl
```

The next `/auto-routines evolve` invocation picks it up.

---

## What "good" looks like after 24 hours

- `.iteration/log.jsonl` has dozens of lines, mostly `ok` / `noop`,
  occasional `err` with a readable `summary`.
- `gh pr list --search "head:routines/"` shows at least one PR per
  installed `forward-driving` routine.
- `/auto-routines status` still prints `state: HEALTHY`.
- No `.iteration/install-failed.md` and no `.iteration/halted.md`.

If you got here without opening troubleshooting once, the install did
its job. From here on, the routines run themselves — you read PRs,
merge or close them, and let the FSM drift the system forward.

If something's off, [troubleshooting](./troubleshooting.md) maps each
failure mode to a concrete fix.
