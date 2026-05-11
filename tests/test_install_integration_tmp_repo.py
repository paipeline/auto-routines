"""
End-to-end integration test for the deterministic half of `init`.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Add an integration test that runs `init` against a fresh temp
    repo under /tmp/auto-routines-test/ and asserts every artifact
    lands on disk (.git/hooks/post-commit exists & executable,
    .claude/skills/<id>/SKILL.md filled with no {{placeholders}},
    .iteration/config.yaml passes sanity-check). Currently the test
    suite only covers the schema and catalog."

This test composes everything shipped in PRs #57, #58, #59, #60:

    1. render-routine-skill        (deterministic placeholder
                                    substitution, no LLM)
    2. install-doctor              (audits a repo's install state)
    3. Mode: doctor wiring         (exposes #2 to the user)

…and proves they actually work together against a real tmp git repo.

What's IN scope:
    - Spin up `tmp_path` as a git repo (`git init`)
    - Write a minimal `config.yaml` (skipping the LLM interview half)
    - Render the shared preamble (just copy from `templates/`)
    - Render each per-routine SKILL.md via `render-routine-skill`
    - Optionally lay down a post-commit hook (when a git-hook routine
      exists) from `templates/post-commit-hook.sh`, `chmod +x`
    - Run `install-doctor` against the result
    - Assert: clean bill of health (rc=0, every check `ok: true`)

What's OUT of scope (separate, larger slice):
    - The interview-driven half of `init` (asking the user which
      routines to install, picking budget tier, etc.) — that needs a
      Claude harness, not just subprocess wiring.
    - Scheduled-task creation via MCP — that's a side-effectful call
      to an external system; tested via `tests/test_open_pr.py`-style
      mocking elsewhere.
    - GHA workflow file generation.

What this test DOES guarantee: the deterministic FILESYSTEM-WRITES
half of `init` produces a passing `install-doctor` audit. Drift in
any of the wrappers (render-routine-skill, install-doctor, the
templates) breaks this test.
"""
from __future__ import annotations

import importlib.util
import io
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"


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
# Helpers — drive the deterministic install steps against a tmp repo
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """Initialize a fresh git repo. We need .git/ for the post-commit
    hook check path and for `install-doctor`'s heuristics."""
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        check=True,
        capture_output=True,
    )


def _write_config(repo: Path, routines: list[dict]) -> Path:
    """Write a minimal `.iteration/config.yaml`. Skips the interview
    half — tests pass in the routines directly."""
    import yaml
    cfg = {
        "schema_version": 4,
        "repo_slug": "tmp-integration-repo",
        "goal": "Integration test for the deterministic install.",
        "mode": "goal-driven",
        "routines": routines,
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "default_stagnation_threshold": 7,
            "anti_flap_window": 3,
            "budget": "medium",
            "idle_window": "always",
            "gha_minutes_cap": 60,
            "kill_switch": False,
        },
    }
    config_path = repo / ".iteration" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


def _render_preamble(repo: Path) -> Path:
    """Step 6f of SKILL.md install. Currently a verbatim copy from
    `templates/routine-preamble.md` — when the preamble renderer
    becomes its own subcommand (future slice), this helper switches
    to invoking it."""
    src = TEMPLATES_DIR / "routine-preamble.md"
    dst = repo / ".claude" / "skills" / "_shared" / "preamble.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"))
    return dst


def _render_routine_skill(orch, repo: Path, routine_id: str) -> Path:
    """Step 6c of SKILL.md install (scheduled / pr-poll branch) —
    invoke the wrapper that PR #57 shipped."""
    dst = repo / ".claude" / "skills" / routine_id / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    rc = orch.cli_main(
        [
            "render-routine-skill",
            "--config", str(repo / ".iteration" / "config.yaml"),
            "--catalog", str(TEMPLATES_DIR / "routine-catalog.yaml"),
            "--template", str(TEMPLATES_DIR / "routine-skill.md"),
            "--routine", routine_id,
            "--out", str(dst),
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert rc == 0, f"render-routine-skill failed for {routine_id!r}"
    return dst


def _install_post_commit_hook(repo: Path) -> Path:
    """Step 6c of SKILL.md install (git-hook branch). Copy the
    canonical template + chmod +x — this is what the install
    procedure does in prose today."""
    src = TEMPLATES_DIR / "post-commit-hook.sh"
    dst = repo / ".git" / "hooks" / "post-commit"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"))
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dst


def _run_install_doctor(orch, repo: Path) -> tuple[int, list[dict]]:
    """Step that the user fires via `/auto-routines doctor`."""
    out = io.StringIO()
    err = io.StringIO()
    rc = orch.cli_main(
        ["install-doctor", "--repo-root", str(repo)],
        stdout=out, stderr=err,
    )
    records = []
    for line in out.getvalue().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            records.append(json.loads(line))
    return rc, records


# Catalog-aligned routine specs. Each archetype id below MUST exist in
# `templates/routine-catalog.yaml` — verified by the render wrapper's
# `archetype not found` error path. If a future evolve removes one of
# these archetypes from the catalog, this test fails loudly (which is
# the correct behavior).
SCHEDULED_ROUTINE = {
    "id": "prd-implement",
    "state": "ACTIVE",
    "primitive": "scheduled",
    "execution_surface": "local",
    "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
    "purpose": "Drive the PRD forward.",
    "success_criterion": "all tasks in goal.md marked done",
    "iter_added": 1,
    "self_evolve": True,
    "automation_level": "auto",
    "prompt_skill": "prd-implement",
}

GIT_HOOK_ROUTINE = {
    "id": "commit-tests",
    "state": "ACTIVE",
    "primitive": "git-hook",
    "trigger": {"human": "on every git commit"},
    "purpose": "Run tests after every commit.",
    "success_criterion": "all tests green for 50 consecutive commits",
    "iter_added": 1,
    "self_evolve": False,
    "automation_level": "auto",
    "prompt_skill": "commit-tests",
}


# ---------------------------------------------------------------------------
# Happy path: scheduled-only install passes install-doctor end-to-end
# ---------------------------------------------------------------------------


class TestScheduledOnlyInstall:
    """The simplest valid install: one scheduled routine, no git-hook,
    no post-commit dispatch needed."""

    def test_full_install_passes_doctor(self, orch, tmp_path):
        """Compose render-routine-skill + install-doctor end-to-end.
        This is the PRD's `init integration test` acceptance criterion
        narrowed to the deterministic half."""
        repo = tmp_path / "iter-038-scheduled-only"
        repo.mkdir()
        _git_init(repo)
        _write_config(repo, [SCHEDULED_ROUTINE])
        _render_preamble(repo)
        _render_routine_skill(orch, repo, SCHEDULED_ROUTINE["id"])

        rc, records = _run_install_doctor(orch, repo)
        assert rc == 0, (
            f"deterministic scheduled-only install must pass "
            f"install-doctor; failing checks: "
            f"{[r for r in records if not r['ok']]}"
        )
        # Spot-check the expected check names are all present.
        check_names = {r["check"] for r in records}
        assert "config-yaml" in check_names
        assert "preamble" in check_names
        assert "routine-skill:prd-implement" in check_names
        assert "post-commit-hook" in check_names  # emitted as n/a

    def test_rendered_skill_md_has_no_placeholders(self, orch, tmp_path):
        """The PRD's explicit failure mode. The render wrapper from
        #57 guarantees this; install-doctor enforces it. This test
        proves the chain works end-to-end against a real tmp repo,
        not just unit-mocked fixtures."""
        repo = tmp_path / "iter-038-placeholder-check"
        repo.mkdir()
        _git_init(repo)
        _write_config(repo, [SCHEDULED_ROUTINE])
        _render_preamble(repo)
        rendered = _render_routine_skill(orch, repo, SCHEDULED_ROUTINE["id"])

        text = rendered.read_text(encoding="utf-8")
        assert "{{" not in text, (
            f"rendered SKILL.md at {rendered} contains `{{{{`; the "
            f"render wrapper should have refused to write — "
            f"investigate how this got past PR #57's check"
        )
        assert "}}" not in text


# ---------------------------------------------------------------------------
# Git-hook variant: post-commit hook is installed and detected
# ---------------------------------------------------------------------------


class TestGitHookInstall:
    """An install with a git-hook routine must also lay down an
    executable post-commit hook. install-doctor's post-commit-hook
    check exercises this path."""

    def test_git_hook_install_passes_doctor(self, orch, tmp_path):
        repo = tmp_path / "iter-038-git-hook"
        repo.mkdir()
        _git_init(repo)
        _write_config(repo, [SCHEDULED_ROUTINE, GIT_HOOK_ROUTINE])
        _render_preamble(repo)
        _render_routine_skill(orch, repo, SCHEDULED_ROUTINE["id"])
        _render_routine_skill(orch, repo, GIT_HOOK_ROUTINE["id"])
        _install_post_commit_hook(repo)

        rc, records = _run_install_doctor(orch, repo)
        assert rc == 0, (
            f"git-hook install must pass install-doctor; failing "
            f"checks: {[r for r in records if not r['ok']]}"
        )

    def test_git_hook_install_without_post_commit_fails(self, orch, tmp_path):
        """Negative case: forget the post-commit hook step. The
        install-doctor check must catch it — this is the
        'half-finished install' failure mode."""
        repo = tmp_path / "iter-038-forgot-hook"
        repo.mkdir()
        _git_init(repo)
        _write_config(repo, [SCHEDULED_ROUTINE, GIT_HOOK_ROUTINE])
        _render_preamble(repo)
        _render_routine_skill(orch, repo, SCHEDULED_ROUTINE["id"])
        _render_routine_skill(orch, repo, GIT_HOOK_ROUTINE["id"])
        # Intentionally skip _install_post_commit_hook.

        rc, records = _run_install_doctor(orch, repo)
        assert rc != 0
        bad = [r for r in records if not r["ok"]]
        assert any(r["check"] == "post-commit-hook" for r in bad), (
            f"missing post-commit hook for a git-hook routine must "
            f"surface in the post-commit-hook check; failing: {bad}"
        )


# ---------------------------------------------------------------------------
# Negative: skip the preamble render and install-doctor catches it
# ---------------------------------------------------------------------------


class TestPreambleSkipped:
    """Drift detector at the integration level: if a future install
    procedure change drops the preamble render step (or moves the
    destination path), install-doctor's `preamble` check fires."""

    def test_preamble_skipped_fails_doctor(self, orch, tmp_path):
        repo = tmp_path / "iter-038-no-preamble"
        repo.mkdir()
        _git_init(repo)
        _write_config(repo, [SCHEDULED_ROUTINE])
        # Intentionally skip _render_preamble(repo).
        _render_routine_skill(orch, repo, SCHEDULED_ROUTINE["id"])

        rc, records = _run_install_doctor(orch, repo)
        assert rc != 0
        check = next(r for r in records if r["check"] == "preamble")
        assert not check["ok"]
        assert "preamble" in check["detail"].lower()
