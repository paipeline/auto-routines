# Troubleshooting

This page covers the common failure modes you'll hit during `init`, what
they look like, and exactly how to fix them. For a happy-path walkthrough
of what to expect when nothing goes wrong, see
[first-24h.md](./first-24h.md).

If `init` halted, the skill wrote one of two diagnostic files:

- `.iteration/install-failed.md` — install ran but at least one artifact
  is missing on disk or in the MCP listing (verify step caught it).
- `.iteration/halted.md` — a dependency health check failed before
  install could start. Next invocation re-checks; deleting the file is
  unnecessary.

Open the diagnostic file first — it names the specific failure. Find
that failure below.

---

## 1. `gh` not authenticated

**You see:**

- `init` halts at the Preflight phase (step 1) with a message like
  `gh auth status: not logged into github.com`.
- Or `.iteration/halted.md` says `gh auth status` failed.

**Why it happens:**

`gh auth status` is one of the Guardrail-4 dependency checks. Without
an authed `gh` we can't list scheduled tasks, can't open routine PRs,
and can't write the GHA workflow's secret. The skill refuses to
continue rather than ship a broken install.

**Fix:**

```bash
gh auth login --git-protocol https --web
```

Pick GitHub.com, follow the browser flow, then re-run `/auto-routines`.
The Guardrail-4 health check reruns at the top of every invocation —
the moment `gh auth status` returns green, install continues from where
it stopped.

If you've authed but a fresh shell isn't picking it up:

```bash
gh auth status              # confirm a token is on file
gh api user                 # confirm the token is live
```

---

## 2. MCP server missing

**You see:**

- `init` halts at the Preflight phase with a message like
  `MCP not connected: scheduled-tasks`.
- Or the interview offers no MCPs to select from in step 4.

**Why it happens:**

`auto-routines` requires the `scheduled-tasks` MCP (per-user, ships
with Claude Code) to register cron-style routines. Other archetypes
optionally use additional MCPs. Without `scheduled-tasks`, no
`primitive: scheduled` routine can be installed.

**Fix:**

Check which MCPs are currently connected:

```bash
claude mcp list
```

The `scheduled-tasks` server should appear with status `running`. If
it's missing, add it via Claude Code's MCP settings (UI: Settings →
MCP Servers, or `claude mcp add scheduled-tasks`). Restart your Claude
Code session so the new MCP is picked up — Preflight re-detects
connected servers on each invocation.

If `scheduled-tasks` is listed but failing, look at the MCP server's
own logs for the underlying issue (usually a missing dependency or a
port conflict). Once it's green:

```bash
claude mcp list             # confirm running
/auto-routines              # re-run init; Preflight passes; install resumes
```

---

## 3. Repo not yet pushed to remote

**You see:**

- `init` halts at step 6g (write GHA workflow) with a message like
  `not a GitHub repo: no remote pointing at github.com`.
- Or `gh secret list` fails with `repository not found`.

**Why it happens:**

The GHA workflow (Module 4, schema-4 install) needs a real GitHub repo
to live in. Step 6g hard-fails when `.git/config` has no remote
pointing at `github.com` — installing a workflow on a local-only repo
would never fire, and `gh secret set` for the `ANTHROPIC_API_KEY` needs
a real repo to attach the secret to.

**Fix:**

Create the GitHub repo and push the initial commit:

```bash
gh repo create <owner>/<name> --private --source=. --remote=origin
git push -u origin main
```

Then re-run `/auto-routines`. Step 6g detects the remote, writes
`.github/workflows/auto-routines.yml`, verifies the `ANTHROPIC_API_KEY`
secret, and install continues.

If the repo exists but the remote isn't set:

```bash
git remote add origin git@github.com:<owner>/<name>.git
git push -u origin main
```

---

## 4. `ANTHROPIC_API_KEY` secret missing

**You see:**

- `.iteration/install-failed.md` ends with:
  ```
  gh secret set ANTHROPIC_API_KEY --repo <owner/name> < /path/to/key
  ```

**Why it happens:**

The GHA workflow's headless Claude step calls the Anthropic API with
this secret. Without it, every scheduled tick fails silently and the
dashboard never sees a routine fire.

**Fix:**

```bash
gh secret set ANTHROPIC_API_KEY --repo <owner>/<name>
# paste your Anthropic API key when prompted
```

Verify it's set:

```bash
gh secret list --repo <owner>/<name>          # ANTHROPIC_API_KEY should appear
```

Re-run `/auto-routines`. The verify step's `gh secret list` check
passes and the workflow installs.

---

## 5. Interpreting `.iteration/install-failed.md`

When step 7 (verify) catches a missing artifact, it writes a
diagnostic file listing exactly what's wrong. Common entries:

| Message                                        | Means                                                     | Fix                                                      |
| ---------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------- |
| `state.json missing`                           | Schema-4 ledger never written.                            | Re-run `init`; step 6 writes it via `state.initial_state()`. |
| `.github/workflows/auto-routines.yml missing`  | Step 6g didn't run (repo isn't a GitHub repo).            | See section 3.                                           |
| `ANTHROPIC_API_KEY not in gh secret list`      | Workflow secret missing.                                  | See section 4.                                           |
| `_shared/preamble.md missing`                  | Step 6f didn't render the shared preamble.                | Re-run `init`; the file regenerates idempotently.        |
| `routine <id>: task_id not in MCP listing`     | A scheduled routine's task ID is in config but the MCP no longer has it. | Run `/auto-routines evolve` — the orphan-sweep guardrail neutralizes it. |
| `routine <id>: SKILL.md has unfilled {{placeholders}}` | Renderer didn't fill every placeholder (catalog drift).   | Run `python3 scripts/render-routine-skills.py` to regenerate. |

If the diagnostic file lists something not in this table, open an
issue with the file's contents — the verify step's check name is
canonical and points directly at the failing assertion in
`scripts/sanity-check.py` or step 7.

---

## 6. The skill keeps printing the plan instead of installing

**You see:**

- Beautiful interview output, beautiful proposed config, then the skill
  exits without writing anything to disk.

**Why it happens:**

This is the failure mode the skill exists to prevent (SKILL.md, top
of file: "Install is mandatory"). If you see it, the agent skipped
step 6. The Guardrails section was either truncated or ignored.

**Fix:**

Re-invoke `/auto-routines` and watch for the streamed phase headers
(`→ [6a] Create .iteration/ skeleton`, `→ [6c] Per-routine install`,
…). If you do NOT see those markers, your version of SKILL.md is
out-of-date — pull latest and try again.

If you DO see the markers but install still produces no artifacts on
disk, run the smoke check:

```bash
ls -la .iteration/                 # should list config.yaml, log.jsonl, etc.
ls .git/hooks/post-commit          # should exist + be executable for git-hook routines
ls .claude/skills/                 # should list one subdir per routine
gh api repos/<owner>/<name>/actions/workflows | jq '.workflows[].name'
# auto-routines should appear in the list
```

If any is missing, the verify step (7) should have caught it — open
`.iteration/install-failed.md` to see exactly which check failed.

---

## Still stuck

Open an issue at https://github.com/paipeline/auto-routines/issues with:

- The contents of `.iteration/install-failed.md` (or `halted.md`)
- Output of `gh auth status`, `claude mcp list`, `git remote -v`
- The first ~20 lines of `.iteration/log.jsonl` (if it exists)

Most halts surface a concrete next command; if the diagnostic file's
message doesn't map to anything above, the verify step's assertion
text is the most useful piece of context to share.
