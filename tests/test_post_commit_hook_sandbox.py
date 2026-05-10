"""
Sandbox tests for templates/post-commit-hook.sh.

PRD `.iteration/goal.md` (Coverage and correctness): "Add a test that
boots the post-commit hook in a sandbox and asserts the background
routines fire (subshell exit code observable via the log)."

The existing `test_catalog.py` checks pin the template's *shape*
(placeholder present, executable, exit-0). These tests pin its
*behavior*: drop the template into a real (temp) git repo, simulate
the install-time splice of `{{routine_dispatch_block}}`, run `git
commit`, and assert the hook fires and the log gets written.

The dispatch block is filled with a SHELL STUB instead of
`claude -p /<routine_id>` — the contract under test is the hook
plumbing (background dispatch, log append, never-block-commit), not
the routine's body.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
HOOK_TEMPLATE = ROOT / "templates" / "post-commit-hook.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _have_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(
    not _have_git(), reason="post-commit sandbox needs `git`"
)


def _init_repo(path: Path) -> None:
    """Initialize a sandbox git repo with deterministic identity. Avoids
    relying on the developer's global git config — CI agents and fresh
    containers don't have one."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@auto-routines.local"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "auto-routines test"],
        cwd=path, check=True,
    )
    # Disable any global hooks-path overrides — we want our installed
    # hook to be the one that fires.
    subprocess.run(
        ["git", "config", "--unset-all", "core.hooksPath"],
        cwd=path, check=False,  # ok if it wasn't set
    )


def _install_hook(repo: Path, dispatch_block: str) -> Path:
    """Splice `dispatch_block` into the template at the
    `{{routine_dispatch_block}}` marker and install as
    `.git/hooks/post-commit`. Returns the installed hook path.

    The template's marker line is `# {{routine_dispatch_block}}` — the
    leading `#` is intentional documentation so the unfilled template
    is still a syntactically valid (no-op) shell script. The install
    step replaces the WHOLE comment line with real dispatch code; we
    mirror that here so what we test is what installs."""
    template = HOOK_TEMPLATE.read_text()
    marker = "# {{routine_dispatch_block}}"
    assert marker in template, (
        "template must contain the documented `# {{routine_dispatch_block}}` "
        "marker line — splice contract regressed"
    )
    hook_text = template.replace(marker, dispatch_block)
    hook_path = repo / ".git" / "hooks" / "post-commit"
    hook_path.write_text(hook_text)
    hook_path.chmod(0o755)
    return hook_path


def _seed_minimal_iteration(repo: Path) -> None:
    """The template early-exits when `.iteration/config.yaml` is
    missing — write a minimal stub so the dispatch block actually runs."""
    iter_dir = repo / ".iteration"
    iter_dir.mkdir(parents=True, exist_ok=True)
    (iter_dir / "config.yaml").write_text(
        "schema_version: 4\nmeta: {}\nroutines: []\n"
    )


def _wait_for_log(log_path: Path, marker: str, timeout: float = 5.0) -> str:
    """Poll for the log file to contain `marker`. Returns the log
    contents on success. Bounded by `timeout` seconds — the dispatch
    runs in a `&` subshell so we have to wait briefly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            text = log_path.read_text()
            if marker in text:
                return text
        time.sleep(0.05)
    raise AssertionError(
        f"timed out after {timeout}s waiting for {marker!r} in "
        f"{log_path} — current contents: "
        f"{log_path.read_text() if log_path.exists() else '<missing>'}"
    )


def _commit(repo: Path, msg: str) -> subprocess.CompletedProcess:
    """Run `git commit --allow-empty` and return the CompletedProcess.
    Caller asserts on return code and stdout/stderr."""
    return subprocess.run(
        ["git", "commit", "--allow-empty", "-m", msg],
        cwd=repo, capture_output=True, text=True, timeout=15,
    )


# ---------------------------------------------------------------------------
# Happy path: a stub routine fires and writes to the log
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_hook_fires_stub_routine_after_commit(self, tmp_path):
        """The canonical contract: a `git commit` triggers the hook, the
        hook dispatches the routine in a background subshell, and the
        subshell appends a line to `.iteration/log.jsonl`."""
        _init_repo(tmp_path)
        _seed_minimal_iteration(tmp_path)

        # Shell stub mimicking the real dispatch shape: subshell, `&`,
        # append a single JSON-ish line. The routine_id token is the
        # canary the test polls for.
        dispatch = (
            '( echo \'{"ts":"sandbox","routine":"stub-routine",'
            '"outcome":"ok"}\' >> "$LOG" ) &\n'
        )
        _install_hook(tmp_path, dispatch)

        result = _commit(tmp_path, "trigger hook")
        assert result.returncode == 0, (
            f"git commit failed; hook must never block a commit. "
            f"stderr: {result.stderr}"
        )

        log = tmp_path / ".iteration" / "log.jsonl"
        text = _wait_for_log(log, "stub-routine")
        assert '"routine":"stub-routine"' in text
        assert '"outcome":"ok"' in text

    def test_multiple_dispatch_lines_all_fire(self, tmp_path):
        """Realistic installs splice ONE dispatch block per git-hook
        routine. Pin that all of them fire from a single commit — not
        just the first."""
        _init_repo(tmp_path)
        _seed_minimal_iteration(tmp_path)

        dispatch = (
            '( echo \'{"routine":"r1","outcome":"ok"}\' >> "$LOG" ) &\n'
            '( echo \'{"routine":"r2","outcome":"ok"}\' >> "$LOG" ) &\n'
            '( echo \'{"routine":"r3","outcome":"ok"}\' >> "$LOG" ) &\n'
        )
        _install_hook(tmp_path, dispatch)
        result = _commit(tmp_path, "multi-routine")
        assert result.returncode == 0

        log = tmp_path / ".iteration" / "log.jsonl"
        # Each routine writes its own line — wait until the last one
        # lands, then assert all three are present.
        text = _wait_for_log(log, '"routine":"r3"')
        for rid in ("r1", "r2", "r3"):
            assert f'"routine":"{rid}"' in text, (
                f"routine {rid!r} missing from log — at least one "
                f"dispatch subshell failed to fire"
            )


# ---------------------------------------------------------------------------
# Non-blocking contract: the hook MUST NOT block commits
# ---------------------------------------------------------------------------
# The template's whole reason to exist as a background dispatcher is
# that a slow / failing routine cannot delay or block the user's
# commit. Pin that contract in three ways.

class TestNonBlocking:
    def test_commit_returns_quickly_even_with_slow_routine(self, tmp_path):
        """A long `sleep` inside the dispatch must not delay the
        commit — the subshell runs in the background.

        Important shell-plumbing contract this test depends on: the
        backgrounded subshell inherits git's stdout/stderr file
        descriptors, so git WILL wait on those fds even though the
        subshell is `&`. The fix (and the convention every real
        dispatch must follow) is `>>"$HOOK_LOG" 2>&1` inside the
        subshell — once stdio is redirected away from git's fds, the
        commit returns immediately. The template's documented example
        dispatch does this; this test pins the contract for any future
        splice."""
        _init_repo(tmp_path)
        _seed_minimal_iteration(tmp_path)

        # Realistic dispatch shape — sleeps long, but redirects stdio
        # to $HOOK_LOG just like the template's documented example.
        dispatch = (
            '( sleep 30; echo \'{"routine":"slow","outcome":"ok"}\' >> "$LOG" )'
            ' >> "$HOOK_LOG" 2>&1 &\n'
        )
        _install_hook(tmp_path, dispatch)

        start = time.time()
        result = _commit(tmp_path, "slow-stub")
        elapsed = time.time() - start
        assert result.returncode == 0
        assert elapsed < 5.0, (
            f"commit took {elapsed:.1f}s — hook must dispatch routines "
            f"in background `&` subshells with stdio redirected so a "
            f"slow routine cannot delay the commit"
        )

    def test_commit_succeeds_when_dispatched_routine_fails(self, tmp_path):
        """A routine that exits non-zero inside its subshell must NOT
        cause the hook to surface a non-zero exit code — `set -u` and
        the ERR trap in the template have to swallow it."""
        _init_repo(tmp_path)
        _seed_minimal_iteration(tmp_path)

        dispatch = (
            '( false; echo \'{"routine":"failing","outcome":"err"}\' '
            '>> "$LOG" ) &\n'
        )
        _install_hook(tmp_path, dispatch)
        result = _commit(tmp_path, "failing-stub")
        assert result.returncode == 0, (
            f"hook returned non-zero ({result.returncode}) when a "
            f"dispatched routine failed; this would block the user's "
            f"commit. stderr: {result.stderr!r}"
        )

    def test_commit_succeeds_when_config_missing(self, tmp_path):
        """Edge case: the user uninstalled `auto-routines` but the
        hook is still on disk (e.g. a git checkout that pulled in
        someone else's .git/hooks). Without a config.yaml the hook
        must early-exit cleanly — not error out."""
        _init_repo(tmp_path)
        # Deliberately do NOT call _seed_minimal_iteration — config is
        # missing.
        dispatch = '( echo \'{"routine":"never","outcome":"ok"}\' >> "$LOG" ) &\n'
        _install_hook(tmp_path, dispatch)

        result = _commit(tmp_path, "no-config")
        assert result.returncode == 0, (
            "hook must exit 0 when .iteration/config.yaml is missing; "
            f"got {result.returncode}, stderr: {result.stderr!r}"
        )
        # Log file must NOT exist — the early-exit guarantees this.
        assert not (tmp_path / ".iteration" / "log.jsonl").exists(), (
            "hook ran the dispatch block despite missing config.yaml — "
            "early-exit clause regressed"
        )


# ---------------------------------------------------------------------------
# Log shape: outcomes are observable via .iteration/log.jsonl
# ---------------------------------------------------------------------------
# The PRD ask explicitly calls out "subshell exit code observable via
# the log" — the log file is the contract surface, not stdout.

class TestLogObservability:
    def test_log_file_is_under_iteration_dir(self, tmp_path):
        """The log path is `.iteration/log.jsonl` per template — pin
        that location so a future refactor doesn't silently move it
        and break every downstream reader (status.py, dashboards,
        etc.)."""
        _init_repo(tmp_path)
        _seed_minimal_iteration(tmp_path)

        dispatch = '( echo \'{"routine":"loc","outcome":"ok"}\' >> "$LOG" ) &\n'
        _install_hook(tmp_path, dispatch)
        _commit(tmp_path, "log-loc")

        _wait_for_log(tmp_path / ".iteration" / "log.jsonl", '"routine":"loc"')

    def test_log_is_append_only(self, tmp_path):
        """Two commits → two log lines. The hook must `>>` not `>`."""
        _init_repo(tmp_path)
        _seed_minimal_iteration(tmp_path)

        dispatch = '( echo \'{"routine":"append","outcome":"ok"}\' >> "$LOG" ) &\n'
        _install_hook(tmp_path, dispatch)

        _commit(tmp_path, "first")
        log = tmp_path / ".iteration" / "log.jsonl"
        _wait_for_log(log, '"routine":"append"')
        first_lines = log.read_text().count('"routine":"append"')

        _commit(tmp_path, "second")
        # Wait for the SECOND line.
        deadline = time.time() + 5.0
        second_lines = first_lines
        while time.time() < deadline:
            second_lines = log.read_text().count('"routine":"append"')
            if second_lines > first_lines:
                break
            time.sleep(0.05)

        assert second_lines == first_lines + 1, (
            f"expected log to gain exactly one line per commit "
            f"(append-only `>>`); had {first_lines}, now {second_lines}"
        )
