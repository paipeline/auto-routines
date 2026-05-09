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
- [ ] Add a `coverage-watcher` archetype: opens a PR when project test coverage
      drops below threshold (per-language detection: pytest-cov, jest --coverage).
- [ ] Add a `pr-review-bot` archetype: posts inline review comments on open PRs
      (style, obvious bugs, security smells).
- [ ] Add a `secret-scan` archetype: catches leaked credentials in a PR and
      blocks merge with a comment.
- [ ] Validate every archetype's `prompt_body` against a fresh temp repo —
      spin one up, run the archetype, assert the expected diff/PR.

### Skill UX
- [ ] Better progress reporting during `init` — currently the user sees a long
      silence then a wall of status. Stream phase headers as we go.
- [x] `/auto-routines status --routine <id>` to drill into one routine's stats.
      (Implemented in `scripts/status.py --routine <id>`, no LLM tokens.)
- [ ] `/auto-routines test-fire <routine_id>` to manually fire one routine
      without waiting for cron — useful for debugging.
- [ ] Surface the first routine PR opened by a fresh install in the welcome
      output ("your first auto-PR will land at ~6:00 PM").

### Dynamic dispatch (added iter-003 — user feedback: each run must include a central agent)
- [x] Replace per-routine cron tasks with one **coordinator** routine that
      reads a structured brief and dispatches the appropriate routine(s) per
      fire. Implemented `scripts/coordinator-brief.py` (pure shell, no LLM
      tokens), `coordinator` archetype in the catalog, and the matching MCP
      scheduled task. `prd-implement` and `session-doc-drift` are now
      dispatched (no own cron). `daily-digest` keeps its 6 PM cron because
      it's time-pinned reporting, not discretionary work.
- [ ] Move `daily-digest` under the coordinator too: extend the coordinator
      cron to include 6 PM (e.g. `0 6,18 * * *`) and gate the digest
      dispatch on time-of-day from inside the brief. Removes the last
      non-coordinator scheduled task.
- [ ] Add a coordinator stagnation rule to the brief: if the last N
      coordinator decisions were all `noop`, surface a banner so the user
      knows the system is idle (vs. silently churning).
- [ ] Document the coordinator/dispatcher pattern in README and
      SKILL.md (new section: "How a fire flows").

### Token frugality continued (PRD #8 — extract shared SKILL.md preamble)
Tracking issue: https://github.com/paipeline/auto-routines/issues/8
Each rendered per-routine `SKILL.md` is ~8.5KB today, ~5KB of which is reusable
boilerplate (FSM, output format, self-evolve, automation_level, PR recipe).
Extract into `.claude/skills/_shared/preamble.md`; per-routine SKILLs shrink to
~2.5KB. Modules per the PRD (each is a candidate slice — implement in this
order so each PR is independently shippable):

- [ ] **Slice A — Shared preamble file.** Create `templates/routine-preamble.md`
      with the FSM, output schema, automation_level dispatch, self-evolve JSON,
      PR recipe, and failure modes (lifted verbatim from the current
      `templates/routine-skill.md`). No renderer changes yet — just the new
      template file plus content tests in `tests/test_preamble.py` covering:
      sections present, no `{{placeholders}}`, verbatim local-time rule pinned.
- [ ] **Slice B — Slim per-routine template.** Trim `templates/routine-skill.md`
      to ~35 lines: keep frontmatter, Purpose, Trigger, Success criterion,
      Inputs, prompt body, and a one-line `## Reference` pointer to
      `.claude/skills/_shared/preamble.md`. Remove the boilerplate sections that
      moved to the preamble. **Fix the double-bullet bug as part of this slice**
      (drop the leading `- ` on the inputs line — see issue body).
- [ ] **Slice C — Renderer update.** `scripts/render-routine-skills.py` writes
      `.claude/skills/_shared/preamble.md` once (idempotent), then renders the
      slimmer per-routine SKILLs. Add `tests/test_render.py` asserting the
      preamble lands and per-routine SKILL.md size < 3KB. Re-render all six
      installed SKILLs and commit the result.
- [ ] **Slice D — Sanity-check byte budget.** Add a post-render rule to
      `scripts/sanity-check.py`: per-routine rendered SKILL.md must be
      <= `meta.max_routine_skill_bytes` (default 3000). Per-routine override
      via `routines[i].max_skill_bytes`. Tests parametrized like the existing
      budget-tier tests in `tests/test_sanity_check.py`.
- [ ] **Slice E — Install flow + verification.** Update `SKILL.md` step 6 to
      install the shared preamble and step 7 to verify it exists with no
      placeholders. Add `.claude/skills/_shared/preamble.md` to the "Files this
      skill manages" section.

### Token frugality (added iter-002 — user feedback: skill is consuming too many tokens)
- [x] `/auto-routines status` MUST be a pure-script call with no Claude tokens.
      Added `scripts/status.py`; SKILL.md `Mode: status` now invokes it directly.
      Tests in `tests/test_status.py` pin the no-subprocess/no-network contract.
- [x] Add `meta.budget: low|medium|high|custom` to schema-3 with a cadence
      preset table in SKILL.md. Self-hosted install bumped to `medium`.
- [ ] Install step 6a should copy `scripts/status.py` from the skill directory
      into the consumer repo so `/auto-routines status` works there too.
      Currently the script only ships in this repo — consumers need it copied
      relative to their repo root. Add to step 6a; cover with an integration
      test that runs `init` against a temp repo and checks the script lands.
- [ ] `daily-digest` low/medium tier — provide a pure-shell variant that skips
      Claude entirely (just `git log` + `gh pr list` formatted as Markdown).
      Catalog should branch on `meta.budget`.
- [ ] Add a `/auto-routines budget <tier>` command that re-applies the cadence
      preset table to the live config + scheduled tasks. Lets the user dial up
      or down without re-running the full interview.
- [ ] Trim the per-routine SKILL.md preamble. The current rendered template
      is ~3KB of boilerplate per fire; extract the FSM/state-handling section
      into a single shared file the routine can `cat` once at start.

### Documentation
- [ ] Write a "first 24 hours" walkthrough in `docs/first-24h.md`.
- [ ] Add a troubleshooting page covering the common install failures
      (`gh` not authed, MCP missing, repo not yet pushed to remote).
- [ ] Annotate `templates/routine-catalog.yaml` with a header block listing
      which archetypes are reactive vs. forward-driving (so the interview can
      group them in the candidate list).

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
