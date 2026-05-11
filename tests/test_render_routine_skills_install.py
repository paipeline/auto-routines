"""
Tests for `scripts/render-routine-skills.py` — the one-shot renderer
used during self-hosted install.

PRD #10 / slice 1 (issue #94, "Preamble extraction + slim per-routine
SKILL template") pins three contracts on this renderer beyond
deterministic placeholder substitution:

  3. The renderer must install
     `templates/routine-preamble.md` → `.claude/skills/_shared/preamble.md`
     (idempotent across re-runs — the per-routine SKILL.md's
     `## Reference` block points here, so identical bytes survive
     across every fire and stay cache-hot).

  4. A per-routine SKILL.md that exceeds `meta.max_routine_skill_bytes`
     (default 3000) must fail the render. Without an enforced cap,
     the boilerplate-extraction win silently regresses as routines
     grow.

  5. A routine may opt out of the cap by setting
     `routines[].max_skill_bytes: <int>` — for genuinely large
     prompt_bodies (e.g. `commit-tests` at 4.4KB). This must be
     a per-routine override, not a global toggle.

The tests below run the renderer against synthetic configs in tmp
dirs so they don't perturb the self-hosted .claude/skills/ output.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render-routine-skills.py"
PREAMBLE_SRC = ROOT / "templates" / "routine-preamble.md"
CATALOG_SRC = ROOT / "templates" / "routine-catalog.yaml"
TEMPLATE_SRC = ROOT / "templates" / "routine-skill.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage_repo(tmp_path: Path, routines: list[dict], meta_extra: dict | None = None) -> Path:
    """Stage a tmp 'repo' with the same layout `render-routine-skills.py`
    expects (templates/, .iteration/config.yaml). Returns the repo root.

    We materialize fresh copies of templates/ so each test can vary
    config.yaml independently without polluting the real repo's files.
    """
    (tmp_path / "templates").mkdir()
    shutil.copy(PREAMBLE_SRC, tmp_path / "templates" / "routine-preamble.md")
    shutil.copy(CATALOG_SRC, tmp_path / "templates" / "routine-catalog.yaml")
    shutil.copy(TEMPLATE_SRC, tmp_path / "templates" / "routine-skill.md")

    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "render-routine-skills.py")

    (tmp_path / ".iteration").mkdir()
    meta = {
        "cron": "0 9 * * *",
        "human": "9:00 AM daily",
        "anti_flap_window": 7,
        "default_stagnation_threshold": 7,
        "process_evolve_requests": True,
    }
    if meta_extra:
        meta.update(meta_extra)
    config = {
        "schema_version": 3,
        "repo_slug": "test-repo",
        "goal": "test",
        "mode": "goal-driven",
        "created_at": "2026-05-11T00:00:00+0200",
        "last_iter": 1,
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": routines,
        "meta": meta,
    }
    (tmp_path / ".iteration" / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False)
    )
    return tmp_path


def _run_renderer(repo: Path) -> subprocess.CompletedProcess:
    """Invoke the staged renderer from the tmp repo root."""
    return subprocess.run(
        [sys.executable, "scripts/render-routine-skills.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _small_routine(rid: str = "commit-lint") -> dict:
    """A routine whose archetype has a small prompt_body — fits under
    the default 3000-byte cap. commit-lint's body is ~1.1KB."""
    return {
        "id": rid,
        "state": "ACTIVE",
        "enabled": True,
        "primitive": "git-hook",
        "trigger": {"human": "on every git commit"},
        "purpose": "Run linters after every commit.",
        "success_criterion": "",
        "stagnation_threshold": 7,
        "self_evolve": False,
        "automation_level": "auto",
        "prompt_skill": rid,
        "iter_added": 1,
    }


def _large_routine(rid: str = "commit-tests") -> dict:
    """A routine whose archetype has a large prompt_body — exceeds the
    default 3000-byte cap. commit-tests' body is ~4.4KB."""
    return {
        "id": rid,
        "state": "ACTIVE",
        "enabled": True,
        "primitive": "git-hook",
        "trigger": {"human": "on every git commit"},
        "purpose": "Run pytest after every commit.",
        "success_criterion": "",
        "stagnation_threshold": 7,
        "self_evolve": False,
        "automation_level": "auto",
        "prompt_skill": rid,
        "iter_added": 1,
    }


# ---------------------------------------------------------------------------
# Contract: shared preamble is installed at .claude/skills/_shared/preamble.md
# ---------------------------------------------------------------------------


class TestPreambleInstall:
    """Acceptance criterion #3 — the renderer installs the shared
    preamble at the path every per-routine SKILL.md's `## Reference`
    section names. Without this step, the install is incomplete: a
    fresh routine fire would find a dangling pointer."""

    def test_preamble_written_to_shared_dir(self, tmp_path):
        repo = _stage_repo(tmp_path, [_small_routine()])
        result = _run_renderer(repo)
        assert result.returncode == 0, (
            f"renderer failed: stderr={result.stderr!r}"
        )
        installed = repo / ".claude" / "skills" / "_shared" / "preamble.md"
        assert installed.exists(), (
            f"renderer must install the shared preamble at "
            f"{installed.relative_to(repo)} — every per-routine "
            f"SKILL.md's `## Reference` block points there"
        )

    def test_installed_preamble_matches_template_bytes(self, tmp_path):
        """The shared preamble is meant to be IDENTICAL bytes across
        every install — that's what makes it cache-hit-able. If the
        renderer mutates the content, cache misses accumulate per fire
        and the token-frugality win evaporates."""
        repo = _stage_repo(tmp_path, [_small_routine()])
        result = _run_renderer(repo)
        assert result.returncode == 0
        installed = repo / ".claude" / "skills" / "_shared" / "preamble.md"
        source = repo / "templates" / "routine-preamble.md"
        assert installed.read_bytes() == source.read_bytes(), (
            "installed preamble must match the template byte-for-byte "
            "so identical content survives across every install and "
            "stays in the prompt cache"
        )

    def test_idempotent_across_reruns(self, tmp_path):
        """Acceptance criterion #3: idempotent across re-runs. The
        renderer is invoked every install AND every time the operator
        edits a prompt_body — running it twice in a row must produce
        the same output as running it once."""
        repo = _stage_repo(tmp_path, [_small_routine()])
        r1 = _run_renderer(repo)
        assert r1.returncode == 0
        installed = repo / ".claude" / "skills" / "_shared" / "preamble.md"
        bytes_after_first = installed.read_bytes()
        mtime_after_first = installed.stat().st_mtime_ns

        r2 = _run_renderer(repo)
        assert r2.returncode == 0
        bytes_after_second = installed.read_bytes()
        # Content stays identical (the cache argument depends on this)
        assert bytes_after_first == bytes_after_second, (
            "two consecutive renders produced different preamble bytes"
        )
        # And the rendered routine SKILL.md also stays identical aside
        # from the installed_at timestamp, which we don't try to pin
        # here — that's an existing test_render_routine_skill.py concern.
        _ = mtime_after_first  # may differ — that's fine, atomic write

    def test_renders_when_shared_dir_already_exists(self, tmp_path):
        """A pre-existing `_shared/` dir (from a previous install)
        must not break the renderer — it should overwrite the
        preamble file in place."""
        repo = _stage_repo(tmp_path, [_small_routine()])
        shared = repo / ".claude" / "skills" / "_shared"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "preamble.md").write_text("# stale content\n")
        result = _run_renderer(repo)
        assert result.returncode == 0, (
            f"renderer failed with pre-existing _shared/: "
            f"stderr={result.stderr!r}"
        )
        # Stale content must be overwritten with the canonical preamble.
        installed = shared / "preamble.md"
        assert "# stale content" not in installed.read_text()
        assert installed.read_bytes() == PREAMBLE_SRC.read_bytes()


# ---------------------------------------------------------------------------
# Contract: byte cap enforced at render time
# ---------------------------------------------------------------------------


class TestByteCapEnforcement:
    """Acceptance criterion #4 — rendered per-routine SKILL.md must
    fail the render when it exceeds `meta.max_routine_skill_bytes`.
    Without enforcement, the boilerplate-extraction win regresses as
    routines slowly grow."""

    def test_small_routine_under_default_cap_succeeds(self, tmp_path):
        repo = _stage_repo(tmp_path, [_small_routine()])
        result = _run_renderer(repo)
        assert result.returncode == 0, (
            f"small routine must render successfully under default "
            f"3000-byte cap; stderr={result.stderr!r}"
        )

    def test_large_routine_over_default_cap_fails(self, tmp_path):
        """A routine whose rendered SKILL.md exceeds the default cap
        must fail the render — no per-routine override, no escape
        hatch."""
        repo = _stage_repo(tmp_path, [_large_routine()])
        result = _run_renderer(repo)
        assert result.returncode != 0, (
            f"large routine must fail render under default cap "
            f"(no override). stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_failure_message_names_routine_and_cap(self, tmp_path):
        """Failure messages must be actionable — the operator needs
        to know WHICH routine and WHAT cap was exceeded to choose
        between trimming prompt_body or adding an override."""
        repo = _stage_repo(tmp_path, [_large_routine("commit-tests")])
        result = _run_renderer(repo)
        assert result.returncode != 0
        # The failure must surface both the offending routine id and
        # the cap value so the operator can act.
        msg = result.stderr + result.stdout
        assert "commit-tests" in msg, (
            f"failure message must name the offending routine, got:\n{msg}"
        )
        assert "3000" in msg or "max_routine_skill_bytes" in msg, (
            f"failure message must name the byte cap or its key, got:\n{msg}"
        )

    def test_meta_override_widens_cap(self, tmp_path):
        """`meta.max_routine_skill_bytes` can be raised to accommodate
        a project whose routines are uniformly larger — without
        forcing per-routine overrides on every entry."""
        repo = _stage_repo(
            tmp_path,
            [_large_routine()],
            meta_extra={"max_routine_skill_bytes": 10000},
        )
        result = _run_renderer(repo)
        assert result.returncode == 0, (
            f"large routine must render successfully when "
            f"meta.max_routine_skill_bytes is raised; "
            f"stderr={result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Contract: per-routine max_skill_bytes override
# ---------------------------------------------------------------------------


class TestPerRoutineOverride:
    """Acceptance criterion #5 — a single routine may opt out of the
    default cap. This matters because typical projects have ONE or
    TWO unusually large prompt_bodies; forcing the global cap up just
    for those routines would silently allow OTHER routines to grow
    unchecked."""

    def test_routine_override_allows_render(self, tmp_path):
        large = _large_routine()
        large["max_skill_bytes"] = 7000
        repo = _stage_repo(tmp_path, [large])
        result = _run_renderer(repo)
        assert result.returncode == 0, (
            f"large routine must render successfully when its own "
            f"max_skill_bytes is raised; stderr={result.stderr!r}"
        )

    def test_routine_override_does_not_widen_other_routines(self, tmp_path):
        """If one routine sets a 10000-byte override, OTHER routines
        in the same config must still be checked against the default
        cap. This is the whole point of per-routine vs. meta-level."""
        large_with_override = _large_routine("commit-tests")
        large_with_override["max_skill_bytes"] = 10000
        another_large = _large_routine("prd-implement")
        # No override on this one — should fail under default cap.
        repo = _stage_repo(tmp_path, [large_with_override, another_large])
        result = _run_renderer(repo)
        assert result.returncode != 0, (
            f"second large routine without override must still fail "
            f"under default cap; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        # And the failure must blame the routine that lacks the override,
        # not the one that has it.
        msg = result.stderr + result.stdout
        assert "prd-implement" in msg

    def test_routine_override_zero_or_negative_is_rejected(self, tmp_path):
        """A 0 or negative override is meaningless and almost always a
        typo. The sanity-check enforces this; pin that the renderer
        also doesn't silently accept it as 'no cap'."""
        # The renderer treats a non-positive override as "no override"
        # and falls back to the meta/default cap — which the large
        # routine still exceeds. So the render fails.
        large = _large_routine()
        large["max_skill_bytes"] = 0
        repo = _stage_repo(tmp_path, [large])
        result = _run_renderer(repo)
        assert result.returncode != 0, (
            "max_skill_bytes=0 must not be treated as an uncapped opt-out"
        )


# ---------------------------------------------------------------------------
# Contract: sanity-check accepts the new fields
# ---------------------------------------------------------------------------


class TestSanityCheckAcceptsNewFields:
    """The new `meta.max_routine_skill_bytes` and per-routine
    `max_skill_bytes` fields must round-trip through sanity-check —
    otherwise sanity-check would reject configs that the renderer is
    happy to consume."""

    def test_sanity_check_accepts_meta_field(self, tmp_path):
        repo = _stage_repo(
            tmp_path,
            [_small_routine()],
            meta_extra={"max_routine_skill_bytes": 3000},
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sanity-check.py"),
             str(repo / ".iteration" / "config.yaml")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"sanity-check rejected meta.max_routine_skill_bytes: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_sanity_check_rejects_negative_meta_field(self, tmp_path):
        repo = _stage_repo(
            tmp_path,
            [_small_routine()],
            meta_extra={"max_routine_skill_bytes": -1},
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sanity-check.py"),
             str(repo / ".iteration" / "config.yaml")],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "max_routine_skill_bytes" in result.stdout

    def test_sanity_check_accepts_routine_override(self, tmp_path):
        small = _small_routine()
        small["max_skill_bytes"] = 5000
        repo = _stage_repo(tmp_path, [small])
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sanity-check.py"),
             str(repo / ".iteration" / "config.yaml")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"sanity-check rejected routines[].max_skill_bytes: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_sanity_check_rejects_negative_routine_override(self, tmp_path):
        small = _small_routine()
        small["max_skill_bytes"] = 0
        repo = _stage_repo(tmp_path, [small])
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "sanity-check.py"),
             str(repo / ".iteration" / "config.yaml")],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "max_skill_bytes" in result.stdout
