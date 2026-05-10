"""
Tests for `orchestrator.py render-routine-skill`.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Add an integration test that runs `init` against a fresh temp
    repo under /tmp/auto-routines-test/ and asserts every artifact
    lands on disk (.git/hooks/post-commit exists & executable,
    .claude/skills/<id>/SKILL.md FILLED WITH NO {{placeholders}},
    .iteration/config.yaml passes sanity-check)."

The full integration test is a separate (bigger) slice — needs a
tmp-repo fixture + Claude harness for the LLM-driven interview steps.
But the **deterministic placeholder substitution** half is mechanical
and was previously done by the LLM in SKILL.md install step 6f:

    "Render templates/routine-skill.md against config values and
     write to .claude/skills/<id>/SKILL.md."

That LLM step fat-fingers placeholders: leftover `{{routine_id}}` in
the rendered file, wrong `installed_at` format (UTC `Z` instead of
local offset), prompt_body pulled from config instead of catalog.

This slice ships a pure-script wrapper that does the substitution
deterministically. The full /tmp/ integration test can then assert
"no `{{placeholders}}` remain" because the wrapper guarantees it.

Contract pinned here:
    render-routine-skill
        --config <config.yaml>
        --catalog <routine-catalog.yaml>
        --template <routine-skill.md>
        --routine <id>
        --out <path>
        [--installed-at <iso>]
"""
from __future__ import annotations

import importlib.util
import io
import re
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


# Canonical template — keep tests self-contained instead of depending
# on the real `templates/routine-skill.md` (its drift is policed by
# `tests/test_routine_preamble.py`, not here).
_TEMPLATE = """\
---
name: {{routine_id}}
description: {{purpose}} — installed at {{installed_at}}, iter-{{iter_added}}.
---

# {{routine_id}}

## Purpose
{{purpose}}

## Trigger
{{trigger_summary}}

## Success criterion
{{success_criterion}}

## Inputs
{{routine_specific_inputs}}

## What to do
{{routine_prompt_body}}

## Self-evolution
{{self_evolve_block}}
"""


def _write_config(tmp_path: Path, routines: list[dict]) -> Path:
    """Build a minimal config.yaml shape. Tests pass routine overrides
    on top of a baseline `prd-implement` entry so each test only sets
    the fields it cares about."""
    import yaml
    config = {
        "schema_version": 4,
        "repo_slug": "test-repo",
        "routines": routines,
    }
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(config))
    return f


def _write_catalog(tmp_path: Path, archetypes: list[dict]) -> Path:
    import yaml
    f = tmp_path / "catalog.yaml"
    f.write_text(yaml.safe_dump({"archetypes": archetypes}))
    return f


def _write_template(tmp_path: Path, body: str = _TEMPLATE) -> Path:
    f = tmp_path / "routine-skill.md"
    f.write_text(body)
    return f


def _baseline_routine(**overrides) -> dict:
    base = {
        "id": "prd-implement",
        "purpose": "Drive PRD forward.",
        "primitive": "scheduled",
        "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
        "success_criterion": "all tasks in goal.md marked done",
        "iter_added": 1,
        "prompt_skill": "prd-implement",
        "self_evolve": True,
    }
    base.update(overrides)
    return base


def _baseline_archetype(**overrides) -> dict:
    base = {
        "id": "prd-implement",
        "prompt_body": "Read goal.md. Pick a slice. Write code + tests. Open PR.\n",
    }
    base.update(overrides)
    return base


def _render(orch, tmp_path: Path, *, routine_overrides=None, archetype_overrides=None,
            template_body: str | None = None, extra_argv: list[str] | None = None,
            stderr: io.StringIO | None = None) -> tuple[int, str, str]:
    """One-shot helper: write config + catalog + template, invoke the
    subcommand, return (rc, out_text, file_text)."""
    routine = _baseline_routine(**(routine_overrides or {}))
    archetype = _baseline_archetype(**(archetype_overrides or {}))
    config_p = _write_config(tmp_path, [routine])
    catalog_p = _write_catalog(tmp_path, [archetype])
    tpl_p = _write_template(tmp_path, template_body or _TEMPLATE)
    out_p = tmp_path / "rendered.md"

    out = io.StringIO()
    err = stderr if stderr is not None else io.StringIO()
    argv = [
        "render-routine-skill",
        "--config", str(config_p),
        "--catalog", str(catalog_p),
        "--template", str(tpl_p),
        "--routine", routine["id"],
        "--out", str(out_p),
    ]
    if extra_argv:
        argv.extend(extra_argv)
    rc = orch.cli_main(argv, stdout=out, stderr=err)
    file_text = out_p.read_text() if out_p.exists() else ""
    return rc, out.getvalue(), file_text


# ---------------------------------------------------------------------------
# Core invariant: no placeholders left after rendering
# ---------------------------------------------------------------------------


class TestNoPlaceholdersAfterRender:
    def test_rendered_file_has_no_double_brace_placeholders(self, orch, tmp_path):
        """The PRD failure mode: a rendered SKILL.md ships with literal
        `{{routine_id}}` because the LLM forgot to substitute it. The
        wrapper must guarantee `{{...}}` never appears in the output."""
        rc, _, text = _render(orch, tmp_path)
        assert rc == 0
        assert "{{" not in text, (
            f"rendered SKILL.md must contain no `{{{{...}}}}` "
            f"placeholders; got:\n{text}"
        )
        assert "}}" not in text

    def test_unknown_placeholder_in_template_is_refused(self, orch, tmp_path):
        """Defensive: if the template (or catalog text) contains a
        placeholder the wrapper doesn't know how to fill, refuse loudly
        rather than write a half-rendered file. Better to fail
        installation than ship a broken SKILL.md to the user."""
        broken = _TEMPLATE + "\n## Mystery\n{{unknown_var}}\n"
        err = io.StringIO()
        rc, _, _ = _render(orch, tmp_path, template_body=broken, stderr=err)
        assert rc != 0, (
            "unknown placeholder must be a hard failure — a silent "
            "leftover would corrupt the user's first-install experience"
        )
        assert "unknown_var" in err.getvalue()


# ---------------------------------------------------------------------------
# Each placeholder maps to the right source
# ---------------------------------------------------------------------------


class TestPlaceholderSources:
    def test_routine_id_comes_from_config(self, orch, tmp_path):
        rc, _, text = _render(
            orch, tmp_path,
            routine_overrides={"id": "my-fancy-routine"},
            archetype_overrides={"id": "my-fancy-routine"},
        )
        assert rc == 0
        assert "my-fancy-routine" in text

    def test_prompt_body_comes_from_catalog_not_config(self, orch, tmp_path):
        """SKILL.md §Placeholder semantics line 723: prompt_body is
        generated from the catalog. Config never contains prompt_body
        (would bloat config.yaml). The catalog is authoritative."""
        rc, _, text = _render(
            orch, tmp_path,
            archetype_overrides={
                "prompt_body": "CATALOG-WINS-HERE: write tests then code.\n",
            },
        )
        assert rc == 0
        assert "CATALOG-WINS-HERE" in text

    def test_trigger_summary_comes_from_trigger_human(self, orch, tmp_path):
        rc, _, text = _render(
            orch, tmp_path,
            routine_overrides={
                "trigger": {"cron": "0 9 * * *", "human": "9 AM daily standup"},
            },
        )
        assert rc == 0
        assert "9 AM daily standup" in text

    def test_success_criterion_fallback_when_empty(self, orch, tmp_path):
        """SKILL.md placeholder table: empty success_criterion renders
        as `(none — runs indefinitely)` — keeps the rendered SKILL.md
        readable instead of leaving a blank line."""
        rc, _, text = _render(
            orch, tmp_path,
            routine_overrides={"success_criterion": ""},
        )
        assert rc == 0
        assert "none" in text.lower() and "indefinitely" in text.lower(), (
            f"empty success_criterion should render the fallback "
            f"`(none — runs indefinitely)`; got:\n{text}"
        )


# ---------------------------------------------------------------------------
# installed_at timestamp format
# ---------------------------------------------------------------------------


class TestInstalledAtTimestamp:
    def test_explicit_installed_at_is_honored(self, orch, tmp_path):
        """Allowing an explicit --installed-at makes the wrapper
        deterministic for testing AND lets a re-install preserve the
        original install timestamp."""
        rc, _, text = _render(
            orch, tmp_path,
            extra_argv=["--installed-at", "2026-05-09T19:17:24+0200"],
        )
        assert rc == 0
        assert "2026-05-09T19:17:24+0200" in text

    def test_default_installed_at_is_local_iso_with_offset_not_z(self, orch, tmp_path):
        """No --installed-at → wrapper uses now() with local offset.
        SKILL.md is explicit: never UTC `Z`. The rendered description
        line is read by humans on their local machines."""
        rc, _, text = _render(orch, tmp_path)
        assert rc == 0
        # Extract the description line (it includes installed_at).
        desc = next(
            (ln for ln in text.splitlines() if "installed at" in ln),
            "",
        )
        assert desc, f"description line missing in:\n{text}"
        # Pull anything that looks like an ISO timestamp from that line.
        m = re.search(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2})",
            desc,
        )
        assert m, f"description line missing ISO timestamp w/ offset: {desc!r}"
        assert "Z" not in m.group(0), (
            f"installed_at must use local offset, never UTC `Z`; got {m.group(0)!r}"
        )


# ---------------------------------------------------------------------------
# self_evolve_block branching
# ---------------------------------------------------------------------------


class TestSelfEvolveBlock:
    def test_block_is_nonempty_when_self_evolve_true(self, orch, tmp_path):
        rc, _, text = _render(
            orch, tmp_path,
            routine_overrides={"self_evolve": True},
        )
        assert rc == 0
        # Section header from template is "## Self-evolution"; the body
        # after it must mention evolve_requests.jsonl (the canonical
        # mid-run evolve channel).
        section = text.split("## Self-evolution", 1)[-1]
        assert "evolve_requests.jsonl" in section, (
            "when routine.self_evolve is true, the self-evolution "
            "section must point at the evolve_requests.jsonl channel"
        )

    def test_block_is_empty_or_noop_when_self_evolve_false(self, orch, tmp_path):
        """If self_evolve is off, the SKILL.md shouldn't tell the
        routine how to append to evolve_requests — confusing
        instruction to follow when the meta won't process it."""
        rc, _, text = _render(
            orch, tmp_path,
            routine_overrides={"self_evolve": False},
        )
        assert rc == 0
        section = text.split("## Self-evolution", 1)[-1]
        # Either the section is empty or it explicitly says "disabled".
        assert "evolve_requests.jsonl" not in section, (
            "when routine.self_evolve is false, the section must NOT "
            "tell the routine to append to evolve_requests.jsonl — "
            "the meta wouldn't process it anyway"
        )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_unknown_routine_id_rejected(self, orch, tmp_path):
        config_p = _write_config(tmp_path, [_baseline_routine()])
        catalog_p = _write_catalog(tmp_path, [_baseline_archetype()])
        tpl_p = _write_template(tmp_path)
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "render-routine-skill",
                "--config", str(config_p),
                "--catalog", str(catalog_p),
                "--template", str(tpl_p),
                "--routine", "does-not-exist",
                "--out", str(tmp_path / "x.md"),
            ],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc != 0
        assert "does-not-exist" in err.getvalue()

    def test_missing_archetype_for_routine_rejected(self, orch, tmp_path):
        """If config references a routine but catalog has no matching
        archetype, prompt_body has nowhere to come from — refuse rather
        than render a SKILL.md with an empty `What to do` section."""
        config_p = _write_config(tmp_path, [_baseline_routine(id="orphan")])
        catalog_p = _write_catalog(tmp_path, [_baseline_archetype()])  # no "orphan"
        tpl_p = _write_template(tmp_path)
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "render-routine-skill",
                "--config", str(config_p),
                "--catalog", str(catalog_p),
                "--template", str(tpl_p),
                "--routine", "orphan",
                "--out", str(tmp_path / "x.md"),
            ],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc != 0
        assert "orphan" in err.getvalue() or "archetype" in err.getvalue().lower()


# ---------------------------------------------------------------------------
# Atomic write — no half-rendered file on failure
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SKILL.md install step 6c must invoke the wrapper, not LLM-render inline
# ---------------------------------------------------------------------------


class TestInstallStep6cInvokesRenderWrapper:
    """The wrapper from this slice only enforces the `no
    `{{placeholders}}`` guarantee if the install procedure actually
    CALLS it. Without this wiring, the install path still hands the
    rendering job to the LLM (which fat-fingers placeholders — the
    original PRD failure mode).

    These pins close the loop: step 6c MUST invoke
    `orchestrator.py render-routine-skill` and MUST NOT instruct the
    LLM to "Read templates/routine-skill.md. Fill all `{{placeholders}}`"
    inline.
    """

    SKILL_MD = ROOT / "SKILL.md"

    def _step_6c_block(self) -> str:
        """Return just the step-6c block — bounded by `**6c.` start
        and the next `**6` heading — so a `render-routine-skill`
        mention in some OTHER section (e.g. a reference table) doesn't
        accidentally satisfy the pin."""
        text = self.SKILL_MD.read_text()
        start = text.find("**6c.")
        assert start != -1, "SKILL.md no longer has a `**6c.` install step"
        # Find the next 6<letter>. step header after 6c.
        m = re.search(r"\*\*6[d-z]\.", text[start + len("**6c."):])
        assert m, "could not find next `**6X.` heading after 6c"
        end = start + len("**6c.") + m.start()
        return text[start:end]

    def test_step_6c_invokes_render_routine_skill_subcommand(self):
        """The wrapper's subcommand name must appear in step 6c's
        per-routine install instructions — otherwise the install LLM
        will keep doing it manually."""
        block = self._step_6c_block()
        assert "render-routine-skill" in block, (
            "SKILL.md step 6c must invoke "
            "`scripts/orchestrator.py render-routine-skill` (the "
            "deterministic substitution wrapper). Without this wiring, "
            "the install LLM falls back to manual placeholder "
            "substitution — the original PRD failure mode "
            "(`filled with no {{placeholders}}`)"
        )

    def test_step_6c_invocation_includes_required_flags(self):
        """Pin the invocation shape. If the SKILL.md drops a required
        flag, the wrapper errors at install time — better to catch the
        drift here than at install."""
        block = self._step_6c_block()
        for flag in ("--config", "--catalog", "--template", "--routine", "--out"):
            assert flag in block, (
                f"SKILL.md step 6c invocation of render-routine-skill "
                f"is missing the required flag `{flag}`. The wrapper "
                f"requires all five — see "
                f"`scripts/orchestrator.py render-routine-skill --help`"
            )

    def test_step_6c_no_longer_tells_llm_to_fill_placeholders(self):
        """Drift detector: if someone re-introduces the manual prose
        ("Fill all `{{placeholders}}`"), the LLM will follow whichever
        instruction it reads first. Remove the fallback so there's
        only one path."""
        block = self._step_6c_block()
        # Tolerate a mention of the WORD "placeholder" (e.g. in a
        # reference comment), but not the imperative "Fill all
        # {{placeholders}}" — that's the LLM-fallback instruction.
        assert "Fill all `{{placeholders}}`" not in block, (
            "SKILL.md step 6c still tells the LLM to manually fill "
            "placeholders. This is the failure mode the wrapper was "
            "supposed to eliminate. Replace the prose with a "
            "`render-routine-skill` invocation."
        )


class TestAtomicWrite:
    def test_no_output_file_when_render_fails(self, orch, tmp_path):
        """When unknown placeholder is detected, the wrapper must NOT
        leave a partial file behind. The tempfile pattern guarantees
        the rename only happens on a fully-rendered string."""
        out_p = tmp_path / "out.md"
        # Pre-condition: file doesn't exist.
        assert not out_p.exists()
        broken = _TEMPLATE + "\n{{unknown}}"
        config_p = _write_config(tmp_path, [_baseline_routine()])
        catalog_p = _write_catalog(tmp_path, [_baseline_archetype()])
        tpl_p = _write_template(tmp_path, broken)
        rc = orch.cli_main(
            [
                "render-routine-skill",
                "--config", str(config_p),
                "--catalog", str(catalog_p),
                "--template", str(tpl_p),
                "--routine", "prd-implement",
                "--out", str(out_p),
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc != 0
        assert not out_p.exists(), (
            "failed render must NOT create the output file — a "
            "half-rendered SKILL.md is worse than no file at all"
        )
