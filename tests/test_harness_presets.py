"""
Tests for stack-aware harness apply — issue #78 (PRD #74).

Two surfaces:

  1. Catalog: `harness_presets` is a new top-level list in
     `templates/routine-catalog.yaml`. Each preset names a stack
     (python-pytest, node-jest, go, ...), the filesystem `stack_hints`
     that identify that stack, and the canonical archetype set to
     install for it.

  2. CLI: `auto-routines detect-harness [--apply]`. Without `--apply`,
     prints the detected stack + canonical archetype set. With
     `--apply`, writes a minimal `.iteration/config.yaml`
     non-interactively so the user can skip the 20-minute interview.

Drift detector: every archetype id referenced by any preset must
exist in the catalog's `archetypes:` list. A phantom id would
silently fail at install time — sanity-check would reject the
generated config (or worse, install a routine the orchestrator
can't render).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"


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


@pytest.fixture(scope="module")
def catalog():
    return yaml.safe_load(CATALOG_PATH.read_text())


# ---------------------------------------------------------------------------
# Catalog shape — harness_presets list exists with required stacks
# ---------------------------------------------------------------------------


class TestHarnessPresetsCatalogShape:
    def test_harness_presets_key_exists(self, catalog):
        assert "harness_presets" in catalog, (
            "templates/routine-catalog.yaml must declare a top-level "
            "`harness_presets:` list — that's the table the "
            "`detect-harness` subcommand reads. Without it, the "
            "express install path has no source of truth."
        )

    def test_required_stacks_present(self, catalog):
        ids = {p["id"] for p in catalog["harness_presets"]}
        for required in ("python-pytest", "node-jest", "go"):
            assert required in ids, (
                f"`harness_presets` is missing required preset "
                f"{required!r}. Issue #78 acceptance criteria pin "
                "these three. Add the preset or update the test if "
                "the schema deliberately changed."
            )

    def test_every_preset_has_required_fields(self, catalog):
        for preset in catalog["harness_presets"]:
            for field in ("id", "stack_hints", "archetypes"):
                assert field in preset, (
                    f"preset {preset.get('id', '<unknown>')!r} is "
                    f"missing required field {field!r}"
                )
            assert isinstance(preset["stack_hints"], list)
            assert isinstance(preset["archetypes"], list)
            assert len(preset["stack_hints"]) >= 1
            assert len(preset["archetypes"]) >= 1


# ---------------------------------------------------------------------------
# Drift detector — preset archetype ids must exist in the catalog
# ---------------------------------------------------------------------------


class TestHarnessPresetArchetypesExist:
    """If a preset names an archetype id that's not in the catalog,
    `--apply` would write a config that sanity-check rejects (or
    worse, an install that fails halfway). Pin this so a typo in a
    preset shows up at test time, not at user-install time."""

    def test_every_preset_archetype_id_exists_in_catalog(self, catalog):
        known_ids = {a["id"] for a in catalog["archetypes"]}
        offenders: list[tuple[str, str]] = []
        for preset in catalog["harness_presets"]:
            for arch_id in preset["archetypes"]:
                if arch_id not in known_ids:
                    offenders.append((preset["id"], arch_id))
        assert not offenders, (
            f"harness preset(s) reference phantom archetype ids "
            f"(preset_id, missing_archetype_id): {offenders}. Known "
            f"archetype ids: {sorted(known_ids)}. Either add the "
            "archetype to `archetypes:` or fix the preset."
        )


# ---------------------------------------------------------------------------
# detect_harness — pure function over a filesystem path
# ---------------------------------------------------------------------------


class TestDetectHarnessFunction:
    """`detect_harness(repo_path, presets)` returns the matching
    preset dict, or None if no preset matches. Pure: no globals, no
    cwd dependence. Detection order matches catalog order — first
    match wins, so the catalog author controls precedence."""

    def test_pytest_repo_detected(self, orch, catalog, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
        (tmp_path / "tests").mkdir()
        out = orch.detect_harness(str(tmp_path), catalog["harness_presets"])
        assert out is not None
        assert out["id"] == "python-pytest"

    def test_node_jest_repo_detected(self, orch, catalog, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "x", "devDependencies": {"jest": "^29"}}\n'
        )
        (tmp_path / "jest.config.js").write_text("module.exports = {};\n")
        out = orch.detect_harness(str(tmp_path), catalog["harness_presets"])
        assert out is not None
        assert out["id"] == "node-jest"

    def test_go_repo_detected(self, orch, catalog, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        (tmp_path / "go.sum").write_text("\n")
        out = orch.detect_harness(str(tmp_path), catalog["harness_presets"])
        assert out is not None
        assert out["id"] == "go"

    def test_empty_repo_returns_none(self, orch, catalog, tmp_path):
        out = orch.detect_harness(str(tmp_path), catalog["harness_presets"])
        assert out is None

    def test_precedence_first_match_wins(self, orch, catalog, tmp_path):
        # If a repo somehow has both python markers AND go markers,
        # the FIRST preset (python-pytest, declared first in the
        # catalog) wins. The catalog author controls priority.
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        out = orch.detect_harness(str(tmp_path), catalog["harness_presets"])
        assert out is not None
        # Both python-pytest and go declare hints that match. The
        # python-pytest preset comes first in the catalog (line check:
        # it's the first entry). So that's what wins.
        assert out["id"] == "python-pytest", (
            "precedence broke: when both stacks match, the FIRST "
            "preset in the catalog should win. If the catalog order "
            "deliberately changed, update this test."
        )


# ---------------------------------------------------------------------------
# CLI — `auto-routines detect-harness [--apply]`
# ---------------------------------------------------------------------------


class TestDetectHarnessCli:
    def test_cli_prints_preset_without_apply(self, orch, tmp_path):
        import io
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
        (tmp_path / "tests").mkdir()
        out = io.StringIO()
        err = io.StringIO()
        code = orch.cli_main(
            [
                "detect-harness",
                "--repo",
                str(tmp_path),
                "--catalog",
                str(CATALOG_PATH),
            ],
            stdout=out,
            stderr=err,
        )
        assert code == 0
        text = out.getvalue()
        assert "python-pytest" in text
        assert "commit-tests" in text  # archetypes listed
        # No file written without --apply.
        assert not (tmp_path / ".iteration").exists()

    def test_cli_apply_writes_config(self, orch, tmp_path):
        import io
        (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
        (tmp_path / "tests").mkdir()
        out = io.StringIO()
        err = io.StringIO()
        code = orch.cli_main(
            [
                "detect-harness",
                "--repo",
                str(tmp_path),
                "--catalog",
                str(CATALOG_PATH),
                "--apply",
            ],
            stdout=out,
            stderr=err,
        )
        assert code == 0, f"stderr was: {err.getvalue()}"
        cfg_path = tmp_path / ".iteration" / "config.yaml"
        assert cfg_path.exists(), (
            "--apply must write `.iteration/config.yaml` non-interactively"
        )
        cfg = yaml.safe_load(cfg_path.read_text())
        # Routines list should equal the preset's archetypes.
        installed_ids = {r["id"] for r in cfg["routines"]}
        catalog = yaml.safe_load(CATALOG_PATH.read_text())
        preset = next(
            p for p in catalog["harness_presets"] if p["id"] == "python-pytest"
        )
        for arch_id in preset["archetypes"]:
            assert arch_id in installed_ids, (
                f"--apply did not install archetype {arch_id!r} from the "
                f"python-pytest preset"
            )

    def test_cli_no_match_exits_nonzero(self, orch, tmp_path):
        import io
        out = io.StringIO()
        err = io.StringIO()
        code = orch.cli_main(
            [
                "detect-harness",
                "--repo",
                str(tmp_path),
                "--catalog",
                str(CATALOG_PATH),
            ],
            stdout=out,
            stderr=err,
        )
        # Non-zero so a shell wrapper sees the failure. Message goes
        # to stderr so stdout stays parseable on success.
        assert code != 0
        assert "no preset matched" in err.getvalue().lower() or "no match" in err.getvalue().lower()
