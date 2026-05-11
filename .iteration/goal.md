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
- [~] Add an integration test that runs `init` against a fresh temp repo under
      `/tmp/auto-routines-test/` and asserts every artifact lands on disk
      (`.git/hooks/post-commit` exists & executable, `.claude/skills/<id>/SKILL.md`
      filled with no `{{placeholders}}`, `.iteration/config.yaml` passes
      sanity-check). Currently the test suite only covers the schema and catalog.
      **Deterministic half shipped end-to-end** in
      `tests/test_install_integration_tmp_repo.py` (5 invariants across
      TestScheduledOnlyInstall, TestGitHookInstall, TestPreambleSkipped):
      composes the shipped wrappers (`render-routine-skill` +
      `install-doctor` + a templates-copy step for the post-commit hook)
      against a real tmp git repo via pytest's `tmp_path`. Asserts the
      full chain produces a clean `install-doctor` audit for both the
      scheduled-only and the git-hook+scheduled configs; negative cases
      pin the failure modes (forgot the post-commit hook → fails;
      forgot the preamble render → fails). Wrappers it composes (all
      previously shipped):
      `scripts/orchestrator.py render-routine-skill` (deterministic
      placeholder substitution; archetype lookup by `routine.id`;
      refuses to write if any `{{...}}` survives; atomic write; pinned
      by `tests/test_render_routine_skill.py`'s 16 invariants);
      `scripts/orchestrator.py install-doctor` (audits config.yaml,
      preamble, per-routine SKILL.md, post-commit hook; JSONL output;
      exit 0 iff all checks pass; pinned by
      `tests/test_install_doctor.py`'s 17 invariants);
      SKILL.md `Mode: doctor` exposes the audit as
      `/auto-routines doctor` (drift-detected). What remains is the
      **LLM-driven half**: an actual Claude-harness test that runs
      `/auto-routines` interview-style against a tmp repo and verifies
      the end-to-end install. That needs a Claude SDK harness or a
      recorded-prompt fixture — separate slice; the deterministic
      assertion target it would compose against is now in place.
- [x] Add tests for the `evolve` flow — drain `evolve_requests.jsonl`, perform
      the FSM transitions, write a checkpoint, apply, verify.
      All five sub-halves shipped — the deterministic evolve pipeline
      is end-to-end CI-covered:
      (a) drain half shipped as `scripts/orchestrator.py drain-evolve-requests`
      (9 invariants in `TestDrainEvolveRequests`). (b) Deterministic FSM
      transition half shipped as `scripts/orchestrator.py fsm-plan` —
      ACTIVE→STAGNANT detector with per-routine + meta-default threshold
      resolution (12 invariants in `TestFsmPlan`); SKILL.md `Mode: evolve`
      step 4 now invokes it. The other transitions (COMPLETED on
      success_criterion-met, reactivation) stay LLM-driven — they
      require natural-language signal interpretation. (c) Checkpoint
      write half shipped as `scripts/orchestrator.py checkpoint-append` —
      pulls iter-number-resolution (`max(existing)+1`) and timestamp
      formatting (local ISO with offset, never UTC `Z`) out of LLM prose
      into a pure-script wrapper. Atomic write via tempfile+os.replace;
      rejects `|` in summary loudly; initializes the canonical Markdown
      table header on fresh files. Pinned by `tests/test_checkpoint_append.py`
      (11 invariants across TestFirstCheckpoint, TestSubsequentAppend,
      TestRowFormat, TestErrorHandling). (d) Apply half shipped as
      `scripts/orchestrator.py apply-fsm-plan` — consumes JSONL plan
      lines (the output of `fsm-plan` or a hand-edited file; `--plan -`
      pipes from stdin so you can chain `fsm-plan ... | apply-fsm-plan
      ...`) and rewrites `routines[i].state` in config.yaml. All-or-
      nothing pre-flight: a single invalid line (unknown routine,
      stale `from`, malformed JSON, missing required field) aborts
      the whole plan WITHOUT touching config.yaml, so half-applied
      configs are impossible. Atomic via `_atomic_write_yaml`; in-place
      mutation preserves untouched routines byte-for-byte. Emits one
      JSON result record per plan line; exit 0 iff every transition
      lands. Pinned by `tests/test_apply_fsm_plan.py` (15 invariants
      across TestApplyHappyPath, TestApplyAtomic, TestApplyValidation,
      TestApplyStdin, TestApplyCli). (e) Verify half shipped as
      `scripts/orchestrator.py verify-fsm-state` — symmetric read-side
      companion to apply. Consumes the SAME JSONL plan as apply;
      treats each line's `to` as the EXPECTED current state and
      asserts the config matches. Output is `{routine_id, expected,
      actual, ok, detail}` matching the install-doctor / apply-fsm-plan
      JSONL convention. A failing assertion does NOT short-circuit —
      every line is evaluated and emitted so the user sees the full
      picture; exit code rolls up to 1 iff any record has `ok:false`.
      Pure read; safe to run repeatedly, mid-evolve, or as an
      independent cron-driven drift check. Pinned by
      `tests/test_verify_fsm_state.py` (12 invariants across
      TestVerifyHappyPath, TestVerifyMismatch, TestVerifyValidation,
      TestVerifyStdin, TestApplyVerifyRoundtrip, TestVerifyCli — the
      round-trip class exercises apply+verify together against one
      shared plan, which is the canonical evolve usage). The PRD's
      evolve flow is now fully wrapped end-to-end: drain → fsm-plan →
      apply-fsm-plan → verify-fsm-state, all deterministic, all CI-
      mocked. SKILL.md install step 6k still uses the prose template
      — harmonizing it to call this wrapper is a separate slice.
- [x] Add a test that boots the post-commit hook in a sandbox and asserts the
      background routines fire (subshell exit code observable via the log).
      Shipped in `tests/test_post_commit_hook_sandbox.py` — 7 invariants
      across TestHappyPath, TestNonBlocking, TestLogObservability. Exposed
      the stdio-redirect contract (git waits on inherited fds even with `&`);
      SKILL.md step 6c now pins it as mandatory.
- [x] Mock the `gh pr create` path in a unit test so CI verifies the call shape
      without needing a real GitHub PR.
      Shipped — `scripts/orchestrator.py open-pr` is the deterministic
      wrapper. Resolves --base from origin's default branch when omitted
      (works on main/master/trunk repos), forbids --repo (in-repo only),
      propagates non-zero exits from both `git symbolic-ref` and `gh pr
      create`. All external commands go through `subprocess.run` so the
      call shape is testable via monkeypatch — no real `gh` invocation,
      no real PR. Pinned by `tests/test_open_pr.py` (8 invariants across
      TestOpenPrCallShape, TestOpenPrErrors, TestNoUnmockedSubprocess).
      Routines can now opt into the wrapper for deterministic PR opening;
      catalog adoption is a separate slice (no archetype rewrites in this
      iteration).

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
- [x] Add a `/auto-routines budget <tier>` command that re-applies the cadence
      preset table to the live config + scheduled tasks. Lets the user dial up
      or down without re-running the full interview.
      Shipped — `scripts/orchestrator.py budget` rewrites config.yaml (atomic)
      AND emits an `mcp-plan:` block of JSON lines for the SKILL.md Mode to
      hand to `mcp__scheduled-tasks__update_scheduled_task`. Pinned by
      `TestBudget` (config) + `TestBudgetMcpPlan` (MCP plan emission).
- [x] Trim the per-routine SKILL.md preamble. The current rendered template
      is ~3KB of boilerplate per fire; extract the FSM/state-handling section
      into a single shared file the routine can `cat` once at start.
      Shipped — `templates/routine-preamble.md` is the canonical shared
      contract (commit/PR procedure, log line format, FSM state-handling
      table, failure modes, mid-run evolve-request shape).
      `templates/routine-skill.md` trimmed from 101 to 41 lines; remaining
      content is purely per-routine + a `## Reference` pointer at
      `.claude/skills/_shared/preamble.md`. SKILL.md install step 6f
      already plans the render. Pinned by `tests/test_routine_preamble.py`
      (14 invariants across 4 classes: existence, no-placeholders, core
      sections covered, json fenced block, drift detectors on the
      routine-skill template). Legacy
      `test_catalog.py::test_routine_skill_template_mandates_branch_and_pr`
      repointed to the preamble file.

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
