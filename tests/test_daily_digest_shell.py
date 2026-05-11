"""
Tests for scripts/daily-digest.sh — the pure-shell daily digest.

PRD `.iteration/goal.md` (Token frugality): the daily-digest archetype
must offer a pure-shell variant for `low`/`medium` budget tiers — no
Claude tokens, just git log + gh pr list rendered as Markdown. Catalog
should be able to branch on `meta.budget` and dispatch this script
instead of an LLM.

These tests pin the shell variant's behavioral contract:

  1. Script exists, is executable, runs with no args without crashing.
  2. Output is valid Markdown with the two canonical sections:
     `## Commits since ...` and `## PR activity (last 24h)`.
  3. Empty git history → stub line, not a crash.
  4. Missing/unauthed gh → stub line, not a crash. Routines must never
     fail the dispatcher just because the operator hasn't run
     `gh auth login`.
  5. `--no-gh` flag short-circuits the PR section entirely (useful in
     CI / hermetic test environments).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "daily-digest.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git(repo: Path) -> None:
    """Create a fresh git repo with deterministic identity."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)


def _commit(repo: Path, name: str, content: str = "x", msg: str | None = None) -> None:
    (repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", msg or f"add {name}"],
        cwd=repo,
        check=True,
    )


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the script inside `repo` with `--no-gh` so tests are hermetic."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Contract: script existence + executability
# ---------------------------------------------------------------------------

class TestScriptArtifact:
    def test_script_exists(self):
        assert SCRIPT.exists(), (
            "scripts/daily-digest.sh must exist — PRD goal: pure-shell "
            "variant of daily-digest for low/medium budget tiers"
        )

    def test_script_is_executable(self):
        assert os.access(SCRIPT, os.X_OK), (
            "scripts/daily-digest.sh must be executable so the catalog "
            "can dispatch it directly without `bash` prefix"
        )

    def test_script_has_shebang(self):
        first_line = SCRIPT.read_text().splitlines()[0]
        assert first_line.startswith("#!"), (
            "script must declare a shebang so chmod +x is enough"
        )
        assert "bash" in first_line or "sh" in first_line


# ---------------------------------------------------------------------------
# Contract: Markdown structure
# ---------------------------------------------------------------------------

class TestOutputStructure:
    def test_runs_on_empty_repo_without_crashing(self, tmp_path):
        """Fresh git repo with no commits must not crash the script.
        The catalog dispatches this script unconditionally on cron —
        any crash means the dashboard misses a digest entry."""
        _init_git(tmp_path)
        result = _run(tmp_path, "00:00 today", "--no-gh")
        assert result.returncode == 0, (
            f"script crashed on empty repo: stderr={result.stderr!r}"
        )

    def test_output_has_date_header(self, tmp_path):
        """Markdown must begin with an H1 dated header so the digest
        is visually distinct when concatenated with other digests."""
        _init_git(tmp_path)
        result = _run(tmp_path, "00:00 today", "--no-gh")
        first_line = result.stdout.splitlines()[0]
        assert first_line.startswith("# Daily digest —"), (
            f"first line must be the date H1; got {first_line!r}"
        )

    def test_output_has_commits_section(self, tmp_path):
        """Commits section must always render, even on empty repo."""
        _init_git(tmp_path)
        result = _run(tmp_path, "00:00 today", "--no-gh")
        assert "## Commits since" in result.stdout

    def test_output_has_pr_activity_section(self, tmp_path):
        """PR activity section must always render (with stub if gh
        unavailable) so the digest shape is consistent."""
        _init_git(tmp_path)
        result = _run(tmp_path, "00:00 today", "--no-gh")
        assert "## PR activity" in result.stdout

    def test_empty_repo_emits_no_commits_stub(self, tmp_path):
        """Empty repo must produce a stub line, not silence — silent
        sections look like the script broke."""
        _init_git(tmp_path)
        result = _run(tmp_path, "00:00 today", "--no-gh")
        assert "(no commits" in result.stdout.lower(), (
            "empty commit window must emit a stub line, got:\n"
            + result.stdout
        )


# ---------------------------------------------------------------------------
# Contract: commits are rendered when present
# ---------------------------------------------------------------------------

class TestCommitRendering:
    def test_commit_appears_in_output(self, tmp_path):
        _init_git(tmp_path)
        _commit(tmp_path, "a.txt", msg="first commit on the digest path")
        result = _run(tmp_path, "1 year ago", "--no-gh")
        assert "first commit on the digest path" in result.stdout, (
            "commit subject must appear in the digest body:\n"
            + result.stdout
        )

    def test_commit_line_uses_bullet_format(self, tmp_path):
        """Lines under the commits section must be Markdown bullets
        (`- <sha> <subject> (<author>)`) — the format SKILL.md's
        Mode: status block expects."""
        _init_git(tmp_path)
        _commit(tmp_path, "a.txt", msg="bullet-format commit")
        result = _run(tmp_path, "1 year ago", "--no-gh")
        # Find the commits section
        lines = result.stdout.splitlines()
        commits_idx = next(
            (i for i, ln in enumerate(lines) if ln.startswith("## Commits")), -1
        )
        assert commits_idx != -1
        # The next non-empty line should start with `- `
        body = [ln for ln in lines[commits_idx + 1 :] if ln.strip()]
        assert body, "commits section was empty even though we committed"
        assert body[0].startswith("- "), (
            f"first commit line must be a Markdown bullet, got {body[0]!r}"
        )


# ---------------------------------------------------------------------------
# Contract: gh failure modes
# ---------------------------------------------------------------------------

class TestGhFailureTolerance:
    def test_no_gh_flag_short_circuits_pr_section(self, tmp_path):
        """`--no-gh` makes the PR section emit a known stub —
        useful for CI / hermetic test envs without a real gh."""
        _init_git(tmp_path)
        result = _run(tmp_path, "00:00 today", "--no-gh")
        assert "(gh skipped" in result.stdout or "skipped" in result.stdout.lower()

    def test_script_does_not_crash_when_gh_missing(self, tmp_path, monkeypatch):
        """If `gh` is not on PATH the script must still exit 0 — the
        whole point of the shell variant is to be a strict superset
        of reliability vs. the LLM variant.

        Strategy: resolve bash + git to absolute paths up front so the
        subprocess doesn't need PATH to find them. Then build a PATH
        whose contents *exclude any directory containing `gh`*. On
        Ubuntu CI bash, git and gh frequently share `/usr/bin`, so
        stripping that directory wholesale would also remove bash —
        hence the absolute-path approach.
        """
        bash_path = shutil.which("bash")
        if bash_path is None:
            pytest.skip("bash not available on PATH — cannot run shell script test")
        # We also need `git` reachable from inside the script.
        git_path = shutil.which("git")
        if git_path is None:
            pytest.skip("git not available on PATH — script needs it")

        # Build a PATH that excludes any directory containing `gh`,
        # but ALWAYS keep the directory holding `git` so the script's
        # internal `git log` invocations still work.
        original_path = os.environ.get("PATH", "")
        git_dir = str(Path(git_path).parent)
        safe_dirs = []
        for d in original_path.split(":"):
            if not d:
                continue
            if (Path(d) / "gh").exists():
                continue
            safe_dirs.append(d)
        if git_dir not in safe_dirs:
            safe_dirs.append(git_dir)
        env = os.environ.copy()
        env["PATH"] = ":".join(safe_dirs)

        _init_git(tmp_path)
        result = subprocess.run(
            [bash_path, str(SCRIPT), "00:00 today"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"script must not crash when gh is missing: "
            f"stderr={result.stderr!r}"
        )
        # And the PR section must still render its header.
        assert "## PR activity" in result.stdout


# ---------------------------------------------------------------------------
# Contract: no LLM, no network beyond gh
# ---------------------------------------------------------------------------

class TestNoLLMContract:
    def test_script_does_not_invoke_claude(self):
        """The whole point of the shell variant is zero Claude tokens.
        Any `claude` invocation in the script is a regression."""
        text = SCRIPT.read_text()
        # Comment lines mentioning claude are fine; real invocations
        # would appear as `claude ` or `claude\n` in a non-comment line.
        non_comment_lines = [
            ln for ln in text.splitlines() if not ln.strip().startswith("#")
        ]
        body = "\n".join(non_comment_lines)
        assert "claude " not in body and "claude\n" not in body, (
            "shell variant must NEVER spawn claude — that's the whole "
            "point of the low/medium budget downgrade"
        )

    def test_script_does_not_invoke_curl_or_wget(self):
        """Pure shell variant should rely only on local git + the gh
        CLI. Random network calls (curl/wget) bypass the cost model."""
        text = SCRIPT.read_text()
        non_comment_lines = [
            ln for ln in text.splitlines() if not ln.strip().startswith("#")
        ]
        body = "\n".join(non_comment_lines)
        assert "curl " not in body
        assert "wget " not in body
