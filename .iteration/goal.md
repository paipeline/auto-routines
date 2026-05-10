# auto-routines — self-improvement goal

> This is the PRD the `prd-implement` routine reads on every fire. When a slice
> ships (PR merged), check it off below. When the list is empty, the routine
> transitions to COMPLETED and you write a fresh `goal.md`.

## North star
Make `auto-routines` a robust skill any developer can run on their repo and get
a working set of self-evolving automations within minutes — without surprises,
broken installs, or routines that drift back to "analyze only."

## Concrete objectives (PRD)

### Coverage and correctness
- [ ] Add an integration test that runs `init` against a fresh temp repo under
      `/tmp/auto-routines-test/` and asserts every artifact lands on disk
      (`.git/hooks/post-commit` exists & executable, `.claude/skills/<id>/SKILL.md`
      filled with no `{{placeholders}}`, `.iteration/config.yaml` passes
      sanity-check). Currently the test suite only covers the schema and catalog.
- [ ] Add tests for the `evolve` flow — drain `evolve_requests.jsonl`, perform
      the FSM transitions, write a checkpoint, apply, verify.
- [ ] Add a test that boots the post-commit hook in a sandbox and asserts the
      background routines fire (subshell exit code observable via the log).
- [ ] Mock the `gh pr create` path in a unit test so CI verifies the call shape
      without needing a real GitHub PR.

### Catalog quality
- [x] Add a `coverage-watcher` archetype: opens a PR when project test coverage
      drops below threshold (per-language detection: pytest-cov, jest --coverage).
      Shipped — see catalog + `tests/test_catalog.py::test_coverage_watcher_*`.
- [x] Add a `pr-review-bot` archetype: posts inline review comments on open PRs
      (style, obvious bugs, security smells).
      Shipped — see catalog + `tests/test_catalog.py::test_pr_review_bot_*`.
- [x] Add a `secret-scan` archetype: catches leaked credentials in a PR and
      blocks merge with a comment.
      Shipped — see catalog + `tests/test_catalog.py::test_secret_scan_*`.
- [ ] Validate every archetype's `prompt_body` against a fresh temp repo —
      spin one up, run the archetype, assert the expected diff/PR.

### Skill UX
- [x] Better progress reporting during `init` — currently the user sees a long
      silence then a wall of status. Stream phase headers as we go.
      Shipped in SKILL.md step 6/7 + `Progress reporting (applies to every
      step)` block; pinned by `TestInitProgressStreaming`.
- [x] `/auto-routines status --routine <id>` to drill into one routine's stats.
      (Implemented in `scripts/status.py --routine <id>`, no LLM tokens.)
- [x] `/auto-routines test-fire <routine_id>` to manually fire one routine
      without waiting for cron — useful for debugging.
      Shipped as pure-script `scripts/orchestrator.py test-fire`; SKILL.md
      `Mode: test-fire` wires it to the slash command. Pinned by
      `TestModeTestFire`.
- [x] Surface the first routine PR opened by a fresh install in the welcome
      output ("your first auto-PR will land at ~6:00 PM").
      Shipped as `scripts/orchestrator.py first-pr-eta`; SKILL.md step 8
      invokes it. Pinned by `tests/test_orchestrator_cli.py::TestFirstPrEta`.

### Token frugality (added iter-002 — user feedback: skill is consuming too many tokens)
- [x] `/auto-routines status` MUST be a pure-script call with no Claude tokens.
      Added `scripts/status.py`; SKILL.md `Mode: status` now invokes it directly.
      Tests in `tests/test_status.py` pin the no-subprocess/no-network contract.
- [x] Add `meta.budget: low|medium|high|custom` to schema-3 with a cadence
      preset table in SKILL.md. Self-hosted install bumped to `medium`.
- [x] Install step 6a should copy `scripts/status.py` from the skill directory
      into the consumer repo so `/auto-routines status` works there too.
      Currently the script only ships in this repo — consumers need it copied
      relative to their repo root. Add to step 6a; cover with an integration
      test that runs `init` against a temp repo and checks the script lands.
      Shipped — SKILL.md step 6a copies `scripts/status.py` via
      `cp "${CLAUDE_SKILL_DIR}/scripts/status.py" scripts/status.py`. Pinned
      by `TestInstallStep6aCopiesStatusScript`. (Live integration test
      against /tmp/ still pending — separate PRD item.)
- [x] `daily-digest` low/medium tier — provide a pure-shell variant that skips
      Claude entirely (just `git log` + `gh pr list` formatted as Markdown).
      Catalog should branch on `meta.budget`.
      Shipped as `scripts/daily-digest.sh` + `shell_variant:` catalog field.
      Pinned by `tests/test_daily_digest_shell.py` + `TestDailyDigestShellVariant`.
- [ ] Add a `/auto-routines budget <tier>` command that re-applies the cadence
      preset table to the live config + scheduled tasks. Lets the user dial up
      or down without re-running the full interview.
- [ ] Trim the per-routine SKILL.md preamble. The current rendered template
      is ~3KB of boilerplate per fire; extract the FSM/state-handling section
      into a single shared file the routine can `cat` once at start.

### Documentation
- [x] Write a "first 24 hours" walkthrough in `docs/first-24h.md`.
      Shipped — 5 milestones (post-install layout, first reactive fire,
      first scheduled tick, first auto-PR, evolve). Pinned by
      `tests/test_first_24h_doc.py`.
- [x] Add a troubleshooting page covering the common install failures
      (`gh` not authed, MCP missing, repo not yet pushed to remote).
      Shipped in `docs/troubleshooting.md`; pinned by
      `tests/test_troubleshooting_doc.py`.
- [x] Annotate `templates/routine-catalog.yaml` with a header block listing
      which archetypes are reactive vs. forward-driving (so the interview can
      group them in the candidate list).
      Shipped in the catalog header (`Archetype categories at a glance`);
      pinned by `tests/test_catalog.py::TestCategoriesAtAGlance`.

## Temp project iteration loop
When `prd-implement` fires and needs to validate a change against a real repo,
it should:

1. Create `/tmp/auto-routines-test/iter-NNN-<slice-slug>/` as a fresh git repo
   (`git init`, write a minimal `pyproject.toml` or `package.json` to mimic
   the target stack).
2. Apply the change (run the install, run the archetype, etc.) against that
   temp repo.
3. Assert the expected artifacts/behaviors.
4. On success: tear the temp repo down. On failure: leave it for inspection
   and reference the path in the PR body.

The temp repo never goes into git. Only the test that proves the change works
goes into the PR.

## Definition of done
A user clones the repo, runs `claude > /auto-routines`, answers ~8 questions,
and 5 minutes later has working scheduled tasks, hooks, and per-routine SKILLs
that begin opening real PRs within hours. No `install-failed.md`, no orphan
tasks, no plans-without-code. The README's "1–100 agents working on your repo
24/7" headline is literally true.
