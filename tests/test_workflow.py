"""
Tests for .github/workflows/auto-routines.yml (PRD #10 Module 4, phase 3).

Per the PRD: one smoke test that parses the YAML and asserts the
contract holds. We don't try to test GHA execution — that's GitHub's
job. We test:

  - The workflow exists at the canonical path.
  - It triggers on the events PRD #10 promised (schedule, pull_request
    closed/merged, issues labeled, push to main, workflow_dispatch,
    repository_dispatch).
  - It calls our two CLI entry points (orchestrator.py tick and
    dashboard.py sync).
  - It pulls ANTHROPIC_API_KEY only from secrets — never hardcoded.
  - State.json gets committed back to the repo so the next tick reads
    fresh state.

These are all things a code reviewer would otherwise catch by eyeball.
A YAML smoke test makes the contract explicit + auto-checks on every
commit.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "auto-routines.yml"


@pytest.fixture(scope="module")
def wf():
    """Parse the workflow YAML once."""
    import yaml
    assert WORKFLOW_PATH.exists(), (
        f"PRD #10 Module 4 requires {WORKFLOW_PATH.relative_to(ROOT)}"
    )
    return yaml.safe_load(WORKFLOW_PATH.read_text())


@pytest.fixture(scope="module")
def wf_text():
    """Raw text — for substring assertions where the YAML structure
    isn't the point (e.g. checking that we run a specific command)."""
    return WORKFLOW_PATH.read_text()


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------

class TestShape:
    def test_workflow_has_a_name(self, wf):
        assert wf.get("name"), "workflow name pinned for gh ui readability"

    def test_workflow_has_at_least_one_job(self, wf):
        assert wf.get("jobs"), "must have a jobs block"
        assert len(wf["jobs"]) >= 1


# ---------------------------------------------------------------------------
# Triggers — PRD #10 Module 4 specifies six trigger types
# ---------------------------------------------------------------------------

class TestTriggers:
    @pytest.fixture
    def on(self, wf):
        # PyYAML parses YAML's `on:` as the boolean True (yes! the keyword
        # is a Norway-problem-adjacent footgun). Workflow YAML has to
        # quote it ('on':) OR we look up by True. Try both.
        return wf.get("on") or wf.get(True)

    def test_on_block_exists(self, on):
        assert on is not None, "workflow needs an `on:` block"

    def test_triggers_on_schedule(self, on):
        assert "schedule" in on, "PRD #10: cron trigger required"
        # Should be a list of {cron: ...} entries
        sched = on["schedule"]
        assert isinstance(sched, list) and len(sched) >= 1
        assert all("cron" in e for e in sched)

    def test_triggers_on_workflow_dispatch(self, on):
        """PRD #10 user story 20: manual tick from `gh workflow run`."""
        assert "workflow_dispatch" in on

    def test_triggers_on_repository_dispatch(self, on):
        """PRD #10 Module 4: programmatic tick (local Stop hook → GHA)."""
        assert "repository_dispatch" in on

    def test_triggers_on_push(self, on):
        """PRD #10 priority rule 2: push to main fires commit-tests."""
        assert "push" in on

    def test_triggers_on_pull_request(self, on):
        """PRD #10 user story 8: PR-merge events fire commit-tests."""
        assert "pull_request" in on

    def test_triggers_on_issues(self, on):
        """PRD #10 priority rule 3: ready-for-agent label fires prd-implement."""
        assert "issues" in on


class TestChangedFilesForwarded:
    """PRD #10 priority rule 4: when goal.md changes in the triggering
    commit, only meta-evolve should fire — not the catch-up commit-tests
    / commit-lint routines. The orchestrator's match_trigger() supports
    this via `path_filters`, gated on a `--changed-files` CLI flag.

    The workflow has to compute the diff and forward it. Without this,
    the priority rule fires path_filters dead from GHA — match_trigger
    falls back to "return all git-hook routines" and the catch-up
    routines steal the slot from meta-evolve.

    These pins ensure:
      1. The workflow computes the changed-file list using git diff
         (so push & merged-PR events both produce a diff list).
      2. The orchestrator step passes `--changed-files` along with
         the trigger type.

    Loose substring matches because the exact shell shape can vary
    (`git diff --name-only`, `git diff-tree`, etc.) but the intent
    must be visibly present.
    """

    def test_workflow_computes_changed_files_for_git_hook_triggers(self, wf_text):
        """Some step must produce a list of repo-relative paths that
        changed in the triggering commit. We grep for either the typical
        plumbing command or a pin on the orchestrator flag — the latter
        proves the data path actually flows to where it matters."""
        has_diff_call = (
            "git diff --name-only" in wf_text
            or "git diff-tree --name-only" in wf_text
        )
        assert has_diff_call, (
            "workflow must compute a changed-file list via "
            "`git diff --name-only` (or equivalent) so the orchestrator's "
            "path_filters short-circuit (PRD #10 rule 4) actually reaches "
            "production triggers"
        )

    def test_orchestrator_tick_step_passes_changed_files_flag(self, wf_text):
        """The flag must show up in the orchestrator tick invocation —
        otherwise the diff is computed but discarded."""
        assert "--changed-files" in wf_text, (
            "workflow computes the changed-file list but never passes it "
            "to orchestrator.py — `--changed-files` flag missing from "
            "the orchestrator tick invocation"
        )


class TestPullRequestMergeGate:
    """PRD #10 user story 8 + dispatch priority rule 2: PR-MERGE events
    fire commit-tests on the merged commit. Not every PR close is a merge —
    a closed-without-merge PR has no diff to test, no SHA in main, no
    coverage gap to fill. Firing the orchestrator on those wastes minutes
    and produces noop ticks at best, confused dispatches at worst.

    The workflow's 'Determine trigger' step must check
    `github.event.pull_request.merged == true` before emitting a
    git-hook trigger for pull_request events. Otherwise commit-tests
    would re-run on every closed-without-merge PR.
    """

    def test_pull_request_step_gates_on_merged(self, wf_text):
        """The shell step that derives trigger type for pull_request events
        must reference `github.event.pull_request.merged` so closed-but-
        not-merged PRs don't trigger downstream routines."""
        assert "github.event.pull_request.merged" in wf_text, (
            "PR-merge gate missing — workflow fires commit-tests on every "
            "PR close, not just merges (PRD #10 user story 8)"
        )

    def test_pull_request_only_subscribes_to_closed(self, wf):
        """We subscribe to `closed` (which fires on both merge and non-merge
        close); the merge-vs-close discrimination happens inside the step.
        This pin ensures we don't accidentally subscribe to `opened` /
        `synchronize` / etc. which would fire commit-tests during PR
        development — the exact noise the gates fixed in PR #24 prevent."""
        # Same Norway-problem dance as TestTriggers.on — `on:` parses to True.
        on = wf.get("on") or wf.get(True)
        pr = on.get("pull_request") or {}
        types = pr.get("types") or []
        assert "closed" in types, "must subscribe to closed events"
        # `opened`, `synchronize`, `reopened`, `edited` etc. would all be
        # premature dispatches — closed is the only event that means
        # "the change is settled."
        for premature in ["opened", "synchronize", "reopened", "edited"]:
            assert premature not in types, (
                f"workflow should NOT fire on PR {premature!r} — that's "
                "in-flight work, not a settled change"
            )

    def test_orchestrator_step_skips_when_trigger_is_skip(self, wf_text):
        """When the gate decides skip (closed-no-merge PR), the orchestrator
        tick step must not run — otherwise we waste a runner spin-up
        executing nothing useful."""
        # Look for a step-level `if:` that references trigger.outputs.type
        # being not-equal-to 'skip'. Loose match — exact YAML formatting
        # varies but the intent must be present.
        assert "skip" in wf_text and "trigger.outputs.type" in wf_text, (
            "missing skip-gate on the orchestrator tick step — "
            "closed-without-merge PRs will still spin up the runner"
        )


# ---------------------------------------------------------------------------
# Job content — must call our CLIs
# ---------------------------------------------------------------------------

class TestCallsOurCLIs:
    def test_runs_orchestrator_tick(self, wf_text):
        """The whole point — workflow invokes our CLI shim."""
        assert "scripts/orchestrator.py" in wf_text
        assert "tick" in wf_text  # subcommand

    def test_runs_dashboard_sync(self, wf_text):
        assert "scripts/dashboard.py" in wf_text
        assert "sync" in wf_text


# ---------------------------------------------------------------------------
# Secrets handling
# ---------------------------------------------------------------------------

class TestSecrets:
    def test_anthropic_api_key_only_from_secrets(self, wf_text):
        """ANTHROPIC_API_KEY must come ONLY from `${{ secrets.ANTHROPIC_API_KEY }}`.
        Any other reference (e.g. hardcoded value, env var, plaintext) is a
        critical leak and fails the test."""
        # Every line that mentions ANTHROPIC_API_KEY must either be
        # the secrets-templated line, or an env-binding referencing it.
        for line in wf_text.splitlines():
            if "ANTHROPIC_API_KEY" not in line:
                continue
            stripped = line.strip()
            # Allowed: either bound from secrets, or a YAML key reference.
            allowed = (
                "secrets.ANTHROPIC_API_KEY" in stripped
                or stripped.startswith("ANTHROPIC_API_KEY:")
                or stripped.startswith("- ANTHROPIC_API_KEY")
                or stripped.startswith("# ")  # comments OK
            )
            assert allowed, (
                f"ANTHROPIC_API_KEY referenced unsafely in workflow: {line!r}"
            )

    def test_no_obvious_plaintext_secret(self, wf_text):
        """A literal sk-ant-... in the YAML would be catastrophic. Pin it."""
        assert "sk-ant" not in wf_text


# ---------------------------------------------------------------------------
# State commit-back — orchestrator writes new state, workflow commits it
# ---------------------------------------------------------------------------

class TestStateCommit:
    def test_workflow_commits_updated_state(self, wf_text):
        """PRD #10: 'commit any updated state.json back to main'.
        Pin that the workflow does it (otherwise next tick reads stale state)."""
        assert "state.json" in wf_text
        # Some form of git commit step expected
        has_commit = "git commit" in wf_text or "git-auto-commit" in wf_text
        assert has_commit, "workflow must commit state.json updates"


# ---------------------------------------------------------------------------
# Permissions — least privilege
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_permissions_block_present(self, wf):
        """Don't rely on the default GITHUB_TOKEN scopes — declare what
        we actually need (issues:write for dashboard, contents:write for
        the state.json commit). Catches accidental over-grant."""
        # Permissions can be at workflow level OR per-job.
        if "permissions" in wf:
            return
        for job in wf.get("jobs", {}).values():
            if "permissions" in job:
                return
        pytest.fail("no permissions: block at workflow or job level")


# ---------------------------------------------------------------------------
# Cache the Claude Code CLI install (OQ1)
# ---------------------------------------------------------------------------
# Cold-installing @anthropic-ai/claude-code on every tick costs ~30s.
# At cron */15 that's ~48 minutes/day of pure waste, eating into the
# meta.gha_minutes_cap (default 60/day) almost entirely on CLI download.
# We pin a version + cache the global install dir so the dispatch step
# becomes ~free on warm runs.

class TestCacheClaudeCli:
    def test_pins_claude_code_version(self, wf_text):
        """Cache key must be deterministic — that means pinning the
        installed version. `latest` would either never invalidate or
        invalidate every tick depending on how the key is built."""
        # Pin lives in a job-level env var so install + cache key both
        # reference the same source of truth.
        assert "CLAUDE_CODE_VERSION:" in wf_text, (
            "expected a CLAUDE_CODE_VERSION env binding in the workflow"
        )

    def test_uses_actions_cache(self, wf_text):
        """We use actions/cache@v4 explicitly (not the setup-node cache:
        'npm' shortcut, which only handles project-local node_modules,
        not global installs)."""
        assert "actions/cache@v4" in wf_text

    def test_cache_key_references_version_pin(self, wf_text):
        """Bumping CLAUDE_CODE_VERSION must invalidate the cache. Otherwise
        the version pin is meaningless — the cache would serve stale
        binaries forever."""
        # Look for the env var being interpolated into a cache key. The
        # cache step has a `key:` field; somewhere on or below it should
        # interpolate ${{ env.CLAUDE_CODE_VERSION }} or equivalent.
        assert "env.CLAUDE_CODE_VERSION" in wf_text, (
            "cache key must interpolate CLAUDE_CODE_VERSION so version "
            "bumps invalidate the cache"
        )

    def test_install_is_conditional_on_cache_miss(self, wf_text):
        """If the cache hits, we should skip `npm install -g` entirely —
        otherwise the cache saves nothing. Look for some form of guard
        (cache-hit conditional or a `command -v` check) around the install."""
        # Two acceptable patterns:
        #   1. step-level `if: steps.<cache>.outputs.cache-hit != 'true'`
        #   2. inline shell guard `if ! command -v claude ...`
        has_step_guard = "cache-hit != 'true'" in wf_text or "cache-hit != \"true\"" in wf_text
        has_shell_guard = (
            "command -v claude" in wf_text
            or "command -v @anthropic-ai/claude-code" in wf_text
            or "if [ ! -x" in wf_text
            or "if ! [ -x" in wf_text
        )
        assert has_step_guard or has_shell_guard, (
            "expected the npm install step to skip on cache hit (either "
            "via a step-level if: cache-hit guard or a shell -x check)"
        )

    def test_setup_node_present(self, wf_text):
        """Caching is moot without a node runtime. Pin actions/setup-node
        so the version is reproducible (the runner default drifts)."""
        assert "actions/setup-node@v4" in wf_text


# ---------------------------------------------------------------------------
# Local dispatch log — append, don't POST to the void (OQ4 phase 2)
# ---------------------------------------------------------------------------
# repository_dispatch is write-only; the previous "Emit local-routine
# dispatches" step POSTed to an unobservable surface. Replace with an
# append-only event log (.iteration/local_dispatches.jsonl) the local
# poller (scripts/local_poller.py) consumes via watermark.

class TestLocalDispatchLog:
    def test_no_repository_dispatch_post_for_local_fires(self, wf_text):
        """The broken 'gh api .../dispatches POST event_type=auto-routines-
        local-fire' lines must be gone. They emit into the void —
        repository_dispatch has no GET endpoint."""
        # The string only ever appeared inside the broken POST step. If
        # it's still here, that step is still wired.
        assert "auto-routines-local-fire" not in wf_text, (
            "the local-fire dispatch POST should be replaced with an "
            "append to .iteration/local_dispatches.jsonl"
        )

    def test_appends_to_local_dispatches_jsonl(self, wf_text):
        """Workflow writes one JSON object per local fire to the event
        log the poller reads."""
        assert ".iteration/local_dispatches.jsonl" in wf_text

    def test_log_uses_event_id_from_state(self, wf_text):
        """event_id must be monotonic + unique. Source of truth is
        state.json's last_event_id (already in the v1 state schema).
        Otherwise the watermark contract breaks."""
        # The shell that builds new entries should reference last_event_id
        # in some form (either reading state.json or computing from it).
        assert "last_event_id" in wf_text

    def test_log_committed_back_with_state(self, wf_text):
        """Same commit-back step that pushes state.json must also push
        the dispatch log — otherwise the poller never sees new entries."""
        # The 'Commit state.json back to main' step must `git add` the
        # dispatch log as well.
        # Look for the path appearing near a `git add` line.
        lines = wf_text.splitlines()
        for i, line in enumerate(lines):
            if "git add" in line and ".iteration/local_dispatches.jsonl" in line:
                return
        # Or as a separate `git add` invocation in the same step.
        # Loosest acceptable check: the path appears AND a `git add` line
        # also references it OR a multi-arg add covers .iteration/ broadly.
        for line in lines:
            if "git add" in line and ".iteration/" in line and "*" in line:
                return
        pytest.fail(
            "commit-back step must `git add .iteration/local_dispatches.jsonl` "
            "(or an equivalent glob) so new fires reach origin/main"
        )

    def test_log_timestamp_uses_offset_not_z(self, wf_text):
        """Poller's parse_log_lines rejects UTC `Z` — the workflow must
        write `+0000` (or any explicit ±HHMM) for the timestamp to be
        accepted."""
        # We look for the workflow shell using `date -u +%FT%T+0000` or
        # equivalent format string. The existing commit message uses
        # `date -u +%FT%TZ` which is FINE for a commit message but would
        # be wrong for the log entries.
        # Just pin: when generating dispatch ts, the format string ends
        # in something other than `Z`.
        assert "+0000" in wf_text or "+%z" in wf_text, (
            "dispatch log entries need an explicit ±HHMM offset; the "
            "poller rejects UTC 'Z' per state.py / SKILL.md"
        )
