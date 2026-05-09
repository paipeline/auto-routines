"""Integration test: artifact-laying phase of `auto-routines init`.

The `init` command is LLM-driven and cannot run in CI. This test verifies the
scriptable subset that `init` delegates to Python scripts:

  1. render-routine-skills.py  →  .claude/skills/<id>/SKILL.md, no {{placeholders}}
  2. sanity-check.py           →  .iteration/config.yaml passes validation
  3. post-commit hook template →  .git/hooks/post-commit exists & is executable

Temp repo: /tmp/auto-routines-test/iter-001-integration-init/
Torn down on success; left for inspection on failure.
"""
from __future__ import annotations

import importlib.util
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from .conftest import ROOT, sanity

# ---------------------------------------------------------------------------
# Load render-routine-skills as an importable module
# ---------------------------------------------------------------------------

RENDER_PATH = ROOT / "scripts" / "render-routine-skills.py"
HOOK_TEMPLATE = ROOT / "templates" / "post-commit-hook.sh"

_render_spec = importlib.util.spec_from_file_location("render_routine_skills", RENDER_PATH)
_render = importlib.util.module_from_spec(_render_spec)
_render_spec.loader.exec_module(_render)

# ---------------------------------------------------------------------------
# Minimal config that mimics what `init` produces for a Python project with
# one scheduled routine (prd-implement) and one git-hook routine (commit-tests)
# ---------------------------------------------------------------------------

MINIMAL_CONFIG: dict = {
    "schema_version": 3,
    "repo_slug": "test-repo",
    "goal": "integration-test project",
    "mode": "goal-driven",
    "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
    "routines": [
        {
            "id": "prd-implement",
            "state": "ACTIVE",
            "primitive": "scheduled",
            "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
            "purpose": "Implement PRD slices on a schedule",
            "automation_level": "auto",
            "self_evolve": True,
            "stagnation_threshold": 5,
            "success_criterion": "all tasks in goal.md marked done",
            "iter_added": 1,
        },
        {
            "id": "commit-tests",
            "state": "ACTIVE",
            "primitive": "git-hook",
            "trigger": {"human": "on every git commit"},
            "purpose": "Run pytest after every commit",
            "automation_level": "auto",
            "self_evolve": False,
            "stagnation_threshold": 7,
            "success_criterion": "all tests green for 50 consecutive commits",
            "iter_added": 1,
        },
    ],
    "neutralized_tasks": [],
    "meta": {
        "cron": "0 9 * * *",
        "human": "9:00 AM daily",
        "anti_flap_window": 3,
        "default_stagnation_threshold": 5,
        "process_evolve_requests": True,
    },
}

TEMP_REPO = Path("/tmp/auto-routines-test/iter-001-integration-init")


@pytest.fixture
def temp_repo(request):
    """Fresh git repo under /tmp/auto-routines-test/ that mimics an `init` install.

    On success: torn down. On failure: preserved at TEMP_REPO for inspection.
    """
    repo = TEMP_REPO
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)

    (repo / "pyproject.toml").write_text("[project]\nname = 'test-repo'\n")

    iteration = repo / ".iteration"
    iteration.mkdir()
    (iteration / "config.yaml").write_text(yaml.dump(MINIMAL_CONFIG, default_flow_style=False))
    (iteration / "log.jsonl").write_text("")
    (iteration / "evolve_requests.jsonl").write_text("")
    (iteration / "checkpoints.md").write_text("# auto-routines checkpoints\n")
    history = iteration / "history"
    history.mkdir()
    (history / "iter-001-init.md").write_text("# iter-001 init\n")

    yield repo

    # Only clean up if the test passed — leave dir for inspection on failure
    outcome = getattr(request.node, "rep_call", None)
    if outcome is None or not outcome.failed:
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_config_passes_sanity_check():
    """MINIMAL_CONFIG itself must pass sanity-check (validates the fixture)."""
    errors = sanity.check(MINIMAL_CONFIG)
    assert errors == [], f"MINIMAL_CONFIG has sanity errors: {errors}"


def test_iteration_dir_artifacts_all_present(temp_repo):
    """.iteration/ skeleton must contain all required files after init."""
    iteration = temp_repo / ".iteration"
    for name in ("config.yaml", "log.jsonl", "evolve_requests.jsonl", "checkpoints.md"):
        assert (iteration / name).exists(), f".iteration/{name} missing"
    assert (iteration / "history" / "iter-001-init.md").exists()


def test_config_yaml_in_temp_repo_passes_sanity_check(temp_repo):
    """The config.yaml written to the temp repo must pass sanity-check."""
    config_path = temp_repo / ".iteration" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    errors = sanity.check(config)
    assert errors == [], f"config.yaml in temp repo failed sanity check: {errors}"


def test_render_skills_creates_files(temp_repo):
    """render_skills() creates one SKILL.md per routine under the output dir."""
    skills_dir = temp_repo / ".claude" / "skills"
    written = _render.render_skills(MINIMAL_CONFIG["routines"], skills_dir)

    assert len(written) == len(MINIMAL_CONFIG["routines"])
    for routine in MINIMAL_CONFIG["routines"]:
        skill_file = skills_dir / routine["id"] / "SKILL.md"
        assert skill_file.exists(), f"{skill_file} was not created"


def test_rendered_skills_have_no_placeholders(temp_repo):
    """Every rendered SKILL.md must have no unfilled {{placeholders}}."""
    skills_dir = temp_repo / ".claude" / "skills"
    _render.render_skills(MINIMAL_CONFIG["routines"], skills_dir)

    for routine in MINIMAL_CONFIG["routines"]:
        text = (skills_dir / routine["id"] / "SKILL.md").read_text()
        snippet = text[text.find("{{") : text.find("}}") + 2] if "{{" in text else ""
        assert "{{" not in text, f"{routine['id']}/SKILL.md has unfilled placeholder: {snippet}"
        assert "}}" not in text, f"{routine['id']}/SKILL.md has stray '}}'"


def test_rendered_skill_reflects_routine_metadata(temp_repo):
    """The prd-implement SKILL.md must include its purpose, trigger, and id."""
    skills_dir = temp_repo / ".claude" / "skills"
    _render.render_skills(MINIMAL_CONFIG["routines"], skills_dir)

    prd = MINIMAL_CONFIG["routines"][0]
    text = (skills_dir / "prd-implement" / "SKILL.md").read_text()
    assert prd["purpose"] in text, "purpose not found in rendered SKILL.md"
    assert prd["trigger"]["human"] in text, "trigger summary not found"
    assert "prd-implement" in text, "routine id not found"


def test_post_commit_hook_is_executable(temp_repo):
    """After copying the hook template, it must exist and be executable."""
    hook_dst = temp_repo / ".git" / "hooks" / "post-commit"
    shutil.copy2(HOOK_TEMPLATE, hook_dst)
    hook_dst.chmod(hook_dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    assert hook_dst.exists(), "post-commit hook not found"
    assert hook_dst.stat().st_mode & stat.S_IXUSR, "post-commit hook is not user-executable"


def test_all_init_artifacts_complete(temp_repo):
    """End-to-end: all three artifact categories land correctly in one shot."""
    # 1. Render skills
    skills_dir = temp_repo / ".claude" / "skills"
    written = _render.render_skills(MINIMAL_CONFIG["routines"], skills_dir)
    assert len(written) == len(MINIMAL_CONFIG["routines"])
    for path in written:
        assert "{{" not in path.read_text()

    # 2. Config passes sanity-check
    config = yaml.safe_load((temp_repo / ".iteration" / "config.yaml").read_text())
    assert sanity.check(config) == []

    # 3. Hook is executable
    hook_dst = temp_repo / ".git" / "hooks" / "post-commit"
    shutil.copy2(HOOK_TEMPLATE, hook_dst)
    hook_dst.chmod(hook_dst.stat().st_mode | stat.S_IXUSR)
    assert hook_dst.stat().st_mode & stat.S_IXUSR
