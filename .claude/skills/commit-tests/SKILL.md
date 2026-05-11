---
name: commit-tests
description: Run pytest after every commit; if it fails, open a fix PR. — installed by auto-routines on 2026-05-11T13:57:34+02:00, iter-1. Invoked by git-hook trigger (on every git commit).
---

# commit-tests

## Purpose
Run pytest after every commit; if it fails, open a fix PR.

## Trigger
on every git commit

## Success criterion
all tests green for 50 consecutive commits

## Inputs to read at fire time
- `.iteration/config.yaml` — read your own entry under `routines:`. Honor `automation_level` and `state`.
- Recent `git log` since last fire of this routine (look up your `last_fire_sha` in `.iteration/log.jsonl`).
- `git show HEAD --stat` and `git show HEAD -- <changed files>` for the just-committed change.
- The pytest output (run `pytest -q` with a 5-minute timeout).

## What to do

**This is not a planning skill. You produce real diffs, not analysis.**

The user just committed. Your job:

0. **Relevance gates — skip when CI would catch it anyway.**
   Most repos already run pytest on push via CI (`.github/workflows/ci.yml`
   or equivalent). This routine earns its keep by being a *fast local
   feedback loop on real code changes* — not by duplicating CI on every
   commit. Apply these gates BEFORE running anything:

   a. **WIP commits.** If the HEAD commit message matches
      `^[Ww][Ii][Pp]\b` or `^wip:` (i.e. the user explicitly marked it
      mid-flow), log `outcome: noop, increment_signal: false,
      summary: "skipped — WIP commit"` and exit. The user does not want
      pytest noise on a checkpoint they already labeled incomplete.

   b. **Docs-only commits.** Run `git show HEAD --name-only --pretty=format:`
      and check the changed files. If every changed path matches
      `*.md`, lives under `docs/`, or is otherwise non-source
      (e.g. `LICENSE`, `*.txt`, `*.rst`, `*.adoc`, image assets,
      `.gitignore`), log `outcome: noop, increment_signal: false,
      summary: "skipped — docs-only commit (N files)"` and exit.
      Pytest cannot fail on prose changes; CI catches anything else.

   These gates exist *because* of CI overlap, not in spite of it.
   Without them this routine burns minutes re-testing every README
   tweak. Do not remove them without revisiting PRD #10 OQ5.

1. Detect the test runner from the repo (package.json scripts.test,
   pyproject.toml [tool.pytest], Cargo.toml, go.mod, Gemfile, etc.).
2. Run it with a 5-minute timeout. Capture stdout+stderr.
3. If exit code is 0:
   a. **Coverage-gap fill — the value-add over CI.** CI already proves
      tests pass; this routine earns its minutes by closing diff
      coverage gaps. Run the test suite with coverage measurement
      (`pytest --cov=. --cov-report=term-missing`, `jest --coverage`,
      `go test -cover`, `cargo tarpaulin`, etc.). Compare against
      the just-committed diff: which lines added in HEAD are NOT
      covered by any test? Use `git diff HEAD~1 HEAD --unified=0`
      to enumerate the new lines, then intersect with the coverage
      tool's missed-lines report.
   b. If every new line is covered: log `outcome: ok,
      increment_signal: false, summary: "diff fully covered"` and exit.
   c. If there are uncovered new lines, pick the 1–3 most behavior-
      critical functions touched (those with branches, side effects,
      or external I/O — same heuristic as `session-test-gap`). Write
      tests that exercise the happy path + one failure path. Match
      the project's existing test style.
   d. Run the new tests; iterate until green and the previously
      uncovered lines are now covered.
   e. Create branch `routines/commit-tests`, commit with message
      `test: cover diff from <short SHA>` listing which functions
      were newly tested.
   f. Open a PR via `python3 scripts/orchestrator.py open-pr
      --head routines/commit-tests --title 'test: cover diff from
      <short SHA>' --body '<list of newly-tested functions, coverage
      delta, link to triggering commit>'`. The wrapper auto-resolves
      `--base` from origin's default branch.
   g. Log `outcome: ok, increment_signal: true, summary: <PR url>`.
   h. Hard cap: 15 minutes for the gap-fill phase. If you hit the
      cap with partial coverage, commit what you have with a TODO
      checklist in the PR body for the rest. Better partial than
      nothing.
4. If exit code is non-zero:
   a. Read the failing test output and the diff of the just-committed change
      (`git show HEAD --stat` and `git show HEAD -- <changed files>`).
   b. Identify the root cause. If it's a clear regression in the committed
      change, write the minimum fix. If it's a flaky test, add `@pytest.mark.flaky`
      (or framework equivalent) only when you can show 3-of-5 runs pass.
   c. Create branch `routines/commit-tests`, commit your fix with message
      `fix(tests): <one-line summary>` referencing the original commit SHA.
   d. Open a PR with `python3 scripts/orchestrator.py open-pr
      --head routines/commit-tests --title 'fix(tests): <summary>'
      --body '<failing test output, root cause, link to triggering
      commit>'`. The wrapper auto-resolves `--base` from origin's
      default branch.
   e. Log `outcome: ok, increment_signal: true, summary: <PR url>`.
5. Never push to main. Never amend the user's commit.


## Self-evolution
(self-evolve not enabled for this routine — your config is fixed by the user. Do not write to `evolve_requests.jsonl`.)

## Reference

All universal rules — commit/branch/push/PR procedure, `.iteration/log.jsonl`
line format, state-handling (which states fire vs. noop), failure modes,
and the mid-run evolve-request shape — live in the **shared preamble**:

  - `.claude/skills/_shared/preamble.md`

That file is rendered once at install (SKILL.md step 6f) from
`templates/routine-preamble.md`, identical bytes across every routine.
**Read it at the start of every fire** before producing work — it's
the canonical contract you commit / log / handle state against.

If a rule in this per-routine SKILL.md contradicts the shared preamble,
the preamble wins. Per-routine SKILL.md only adds *routine-specific*
content (purpose, trigger, prompt body); never re-declares universal
rules.
