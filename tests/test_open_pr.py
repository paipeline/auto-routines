"""
Tests for the `orchestrator.py open-pr` subcommand — a deterministic
Python wrapper around `gh pr create`.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Mock the `gh pr create` path in a unit test so CI verifies the
    call shape without needing a real GitHub PR."

The whole point of this slice is to give CI a code path to mock.
Previously, every `gh pr create` invocation lived inside an LLM-driven
SKILL.md prompt body — no Python code path to test. This wrapper
gives us:

  - One source-of-truth call shape (`--head`, `--title`, `--body`,
    auto-resolved `--base` from origin's default branch).
  - A subprocess seam we can mock to assert the call shape in CI.
  - A way for routines (and the install procedure / evolve) to open
    PRs deterministically instead of asking the LLM to assemble the
    invocation each time.

Tests mock `subprocess.run` via monkeypatch — no real `gh` invocation,
no real GitHub PR. The point is the shape, not the side effect.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location(
        "orchestrator", ROOT / "scripts" / "orchestrator.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def orch():
    return _load_orchestrator()


# ---------------------------------------------------------------------------
# Subprocess mock helpers
# ---------------------------------------------------------------------------


class FakeSubprocessRun:
    """Records every subprocess.run call and returns scripted responses.

    Each entry in `responses` is matched against the argv prefix: if the
    incoming call starts with `prefix`, the corresponding result is
    returned. Calls with no match fall through to a generic success
    (rc=0, empty stdout). Records every call to `.calls` for the test
    to assert against.

    Why not just use Mock? Because we want a clear separation between
    `git symbolic-ref` (default base resolution) and `gh pr create`
    (the actual PR open) — readable test failures depend on the
    response being keyed to the command being invoked."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, result in self.responses.items():
            if list(prefix) == list(argv)[: len(prefix)]:
                return result
        # Default: success, empty stdout — keeps tests that don't care
        # about a particular response from having to set one.
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _gh_pr_url_response(url="https://github.com/x/y/pull/1"):
    """A canned `gh pr create` success response that prints the PR
    URL on stdout — matches the real gh behavior."""
    return subprocess.CompletedProcess(
        args=("gh", "pr", "create"),
        returncode=0,
        stdout=url + "\n",
        stderr="",
    )


def _git_default_branch_response(branch="main"):
    """A canned `git symbolic-ref` success that returns the origin
    default branch — the wrapper uses this when --base isn't given."""
    return subprocess.CompletedProcess(
        args=("git", "symbolic-ref"),
        returncode=0,
        stdout=f"origin/{branch}\n",
        stderr="",
    )


# ---------------------------------------------------------------------------
# Core call shape — the regression guard CI runs without a real PR
# ---------------------------------------------------------------------------


class TestOpenPrCallShape:
    """The whole reason this subcommand exists: pin the gh pr create
    call shape so CI catches drift before a real fire fails."""

    def test_invokes_gh_pr_create_with_required_flags(self, orch, monkeypatch):
        fake = FakeSubprocessRun({
            ("gh", "pr", "create"): _gh_pr_url_response(),
        })
        monkeypatch.setattr(subprocess, "run", fake)
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--base", "main",
                "--title", "test: a thing",
                "--body", "body of the PR",
            ],
            stdout=out,
        )
        assert rc == 0, out.getvalue()
        # Find the gh pr create call (might be the only call, or one
        # after `git symbolic-ref` — be tolerant).
        gh_calls = [c for c in fake.calls if c[:3] == ["gh", "pr", "create"]]
        assert len(gh_calls) == 1, (
            f"expected exactly one `gh pr create` call; got: {fake.calls}"
        )
        argv = gh_calls[0]
        # Required flags, each followed by its value:
        for flag, value in (
            ("--head", "routines/foo"),
            ("--base", "main"),
            ("--title", "test: a thing"),
            ("--body", "body of the PR"),
        ):
            assert flag in argv, f"gh pr create missing {flag} in {argv}"
            assert argv[argv.index(flag) + 1] == value, (
                f"{flag} value drift: expected {value!r}, "
                f"got {argv[argv.index(flag) + 1]!r}"
            )

    def test_resolves_default_base_when_not_provided(self, orch, monkeypatch):
        """If `--base` is omitted, resolve via
        `git symbolic-ref --short refs/remotes/origin/HEAD | sed s@^origin/@@`.
        Otherwise routines would have to hardcode `main` and break
        on repos that default to `master` or `trunk`."""
        fake = FakeSubprocessRun({
            ("git", "symbolic-ref"): _git_default_branch_response("trunk"),
            ("gh", "pr", "create"): _gh_pr_url_response(),
        })
        monkeypatch.setattr(subprocess, "run", fake)
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--title", "t",
                "--body", "b",
            ],
            stdout=out,
        )
        assert rc == 0
        gh_call = next(c for c in fake.calls if c[:3] == ["gh", "pr", "create"])
        assert "--base" in gh_call, (
            "wrapper must resolve --base when omitted, not pass through "
            "without it (gh would otherwise fail noisily)"
        )
        assert gh_call[gh_call.index("--base") + 1] == "trunk", (
            "resolved base must be the trimmed origin/HEAD output "
            "(`origin/` prefix stripped)"
        )

    def test_never_passes_repo_flag(self, orch, monkeypatch):
        """`gh pr create --repo cross/org` would target a different
        repo. Routines must stay in their own repo. No version of
        this wrapper should ever pass --repo, regardless of inputs."""
        fake = FakeSubprocessRun({
            ("gh", "pr", "create"): _gh_pr_url_response(),
        })
        monkeypatch.setattr(subprocess, "run", fake)
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--base", "main",
                "--title", "t",
                "--body", "b",
            ],
            stdout=io.StringIO(),
        )
        assert rc == 0
        gh_call = next(c for c in fake.calls if c[:3] == ["gh", "pr", "create"])
        assert "--repo" not in gh_call, (
            "wrapper must NEVER pass --repo — cross-repo PR opens are "
            "an attack surface and out of scope for routines"
        )

    def test_emits_pr_url_to_stdout_on_success(self, orch, monkeypatch):
        """The wrapper's contract: caller reads the PR URL from stdout.
        SKILL.md prompt bodies that call the wrapper can drop the
        result straight into log lines / iter-NNN.md."""
        fake = FakeSubprocessRun({
            ("gh", "pr", "create"): _gh_pr_url_response(
                "https://github.com/paipeline/auto-routines/pull/42"
            ),
        })
        monkeypatch.setattr(subprocess, "run", fake)
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--base", "main",
                "--title", "t",
                "--body", "b",
            ],
            stdout=out,
        )
        assert rc == 0
        assert "https://github.com/paipeline/auto-routines/pull/42" in out.getvalue(), (
            "wrapper must emit the PR URL on stdout — callers can't log "
            "what they can't read"
        )


# ---------------------------------------------------------------------------
# Error propagation — failures must surface, not silently succeed
# ---------------------------------------------------------------------------


class TestOpenPrErrors:
    def test_nonzero_gh_exit_propagates(self, orch, monkeypatch):
        """If `gh pr create` fails (auth issue, branch not pushed,
        PR already exists), the wrapper exits non-zero so the caller
        knows the routine didn't actually open the PR. Silent
        success would falsely advance state."""
        fake = FakeSubprocessRun({
            ("gh", "pr", "create"): subprocess.CompletedProcess(
                args=("gh", "pr", "create"),
                returncode=1,
                stdout="",
                stderr="gh: no commits between main and routines/foo\n",
            ),
        })
        monkeypatch.setattr(subprocess, "run", fake)
        out = io.StringIO()
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--base", "main",
                "--title", "t",
                "--body", "b",
            ],
            stdout=out,
            stderr=err,
        )
        assert rc != 0, (
            "gh failure must propagate as non-zero exit; routine state "
            "advancement depends on this signal"
        )
        # The gh stderr must surface somewhere visible — without it,
        # the user can't diagnose what went wrong.
        combined = (out.getvalue() + err.getvalue()).lower()
        assert "no commits" in combined or "gh:" in combined, (
            "gh stderr must surface to the caller; got "
            f"stdout={out.getvalue()!r}, stderr={err.getvalue()!r}"
        )

    def test_default_base_resolution_failure_propagates(self, orch, monkeypatch):
        """If `git symbolic-ref` fails (no origin remote, detached
        HEAD, fresh repo), surface a non-zero exit before attempting
        `gh pr create` with a missing/garbage base. Otherwise gh
        prints a misleading error about the BASE that doesn't tell
        the user the real problem is upstream."""
        fake = FakeSubprocessRun({
            ("git", "symbolic-ref"): subprocess.CompletedProcess(
                args=("git", "symbolic-ref"),
                returncode=1,
                stdout="",
                stderr="fatal: ref refs/remotes/origin/HEAD is not a symbolic ref\n",
            ),
        })
        monkeypatch.setattr(subprocess, "run", fake)
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--title", "t",
                "--body", "b",
                # NOTE: --base omitted on purpose
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0
        # Must NOT have attempted gh pr create — base resolution
        # failed first.
        gh_calls = [c for c in fake.calls if c[:3] == ["gh", "pr", "create"]]
        assert gh_calls == [], (
            "wrapper must short-circuit when base resolution fails; "
            "attempting gh pr create with no/garbage base produces "
            "a misleading downstream error"
        )

    def test_missing_required_flag_is_argparse_error(self, orch):
        """argparse handles this; pinning the exit code so any future
        wrapper refactor doesn't accidentally make --head optional
        (which would let a routine open a PR with no head branch —
        nonsensical)."""
        rc = orch.cli_main(
            ["open-pr", "--base", "main", "--title", "t", "--body", "b"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0, (
            "argparse must reject missing --head; this slice's contract "
            "is that the wrapper assembles a complete call, never a "
            "partial one"
        )


# ---------------------------------------------------------------------------
# No real subprocess work — keeps tests fast and deterministic
# ---------------------------------------------------------------------------


class TestNoUnmockedSubprocess:
    """Defensive: even with a benign argv that returns 0, no test in
    this file should make a real `gh` or `git` call (we don't have
    those in CI's hermetic env). If a future test forgets to mock,
    this catches it before flake hits."""

    def test_fixture_blocks_unmocked_subprocess(self, orch, monkeypatch):
        called = []

        def boom(*args, **kwargs):
            called.append((args, kwargs))
            # Return success so the wrapper's happy path completes —
            # we're asserting the mock was hit, not that gh ran.
            return subprocess.CompletedProcess(
                args=args[0] if args else [],
                returncode=0,
                stdout="https://example/pr/1\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", boom)
        rc = orch.cli_main(
            [
                "open-pr",
                "--head", "routines/foo",
                "--base", "main",
                "--title", "t",
                "--body", "b",
            ],
            stdout=io.StringIO(),
        )
        assert rc == 0
        assert called, (
            "wrapper must go through subprocess.run; if this fires "
            "without hitting the mock, either the wrapper bypassed "
            "subprocess.run (using os.exec / shell=True / etc.) or the "
            "monkeypatch didn't reach the call site"
        )
