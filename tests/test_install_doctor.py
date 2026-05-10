"""
Tests for `orchestrator.py install-doctor`.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Add an integration test that runs `init` against a fresh temp
    repo under /tmp/auto-routines-test/ and asserts every artifact
    lands on disk (.git/hooks/post-commit exists & executable,
    .claude/skills/<id>/SKILL.md filled with no {{placeholders}},
    .iteration/config.yaml passes sanity-check)."

The full integration test is a separate (bigger) slice — it needs a
tmp-repo fixture + a Claude harness for the LLM-driven interview
steps. This slice ships the **verification half**: a pure-script
audit that checks whether a repo has a healthy auto-routines install.

The audit logic itself is deterministic and testable WITHOUT running
Claude — build a fake "fully installed" repo by hand, run the audit,
and assert the right checks pass / fail. When the full integration
test lands, it'll just need to invoke the install via Claude, then
call install-doctor for the verification half.

Each check emits a single JSON line on stdout:
    {"check": <name>, "ok": <bool>, "detail": <text>}

Exit code:
    0 — every check passed
    1 — at least one check failed (caller diffs the JSONL for which)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
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
# Helpers — build fake "installed" repos at varying degrees of completeness
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _baseline_config(routines: list[dict] | None = None) -> dict:
    """Minimal but valid config.yaml shape. Tests override routines
    when they need a specific primitive mix."""
    return {
        "schema_version": 4,
        "repo_slug": "test-repo",
        "routines": routines if routines is not None else [
            {
                "id": "prd-implement",
                "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
                "purpose": "Drive the PRD forward.",
                "success_criterion": "all tasks done",
                "iter_added": 1,
                "self_evolve": True,
            },
        ],
    }


def _build_full_install(tmp_path: Path, *, config: dict | None = None,
                         routine_skill_body: str | None = None,
                         post_commit_executable: bool = True,
                         create_post_commit: bool = True) -> Path:
    """Build a fake "fully installed" repo and return its root.

    Layout:
        <root>/
            .git/
                hooks/
                    post-commit        (executable if requested)
            .iteration/
                config.yaml
            .claude/
                skills/
                    _shared/
                        preamble.md
                    <routine_id>/
                        SKILL.md       (no placeholders by default)
    """
    cfg = config if config is not None else _baseline_config()
    _write_yaml(tmp_path / ".iteration" / "config.yaml", cfg)

    # Shared preamble.
    preamble_dir = tmp_path / ".claude" / "skills" / "_shared"
    preamble_dir.mkdir(parents=True, exist_ok=True)
    (preamble_dir / "preamble.md").write_text("# preamble\n")

    # Per-routine SKILL.md files, no placeholders by default.
    skill_body = routine_skill_body or "# rendered routine SKILL.md\n"
    for r in cfg["routines"]:
        rid = r["id"]
        skill_dir = tmp_path / ".claude" / "skills" / rid
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill_body)

    # Post-commit hook only if any git-hook routine present, OR caller
    # forces it (so we can test the "post-commit but no git-hook routine"
    # case if needed). Tests that want a missing hook pass
    # create_post_commit=False.
    if create_post_commit:
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "post-commit"
        hook.write_text("#!/bin/sh\necho hi\n")
        if post_commit_executable:
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            # Force a non-executable mode for the negative test.
            hook.chmod(0o644)

    return tmp_path


def _parse_jsonl(text: str) -> list[dict]:
    """Filter blank lines + comment-style `#` lines; parse the rest."""
    out = []
    for ln in text.splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        out.append(json.loads(ln))
    return out


def _run(orch, repo_root: Path, *, extra_argv: list[str] | None = None) -> tuple[int, list[dict], str]:
    out = io.StringIO()
    err = io.StringIO()
    argv = ["install-doctor", "--repo-root", str(repo_root)]
    if extra_argv:
        argv.extend(extra_argv)
    rc = orch.cli_main(argv, stdout=out, stderr=err)
    return rc, _parse_jsonl(out.getvalue()), err.getvalue()


def _check(records: list[dict], name: str) -> dict:
    """Find one check record by name. Tests get a clean assertion
    message if the record is absent vs. the expected ok-value mismatches."""
    matches = [r for r in records if r.get("check") == name]
    assert matches, (
        f"install-doctor did not emit a check named {name!r}; "
        f"emitted: {[r.get('check') for r in records]}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Happy path: a fully-built install passes every check
# ---------------------------------------------------------------------------


class TestFullInstallPasses:
    def test_complete_install_exits_zero(self, orch, tmp_path):
        """A repo with config.yaml + per-routine SKILL.md (no
        placeholders) + preamble + executable post-commit must produce
        a clean bill of health."""
        root = _build_full_install(tmp_path)
        rc, records, _ = _run(orch, root)
        assert rc == 0, (
            f"complete install should exit 0; got rc={rc}, "
            f"failing checks: {[r for r in records if not r.get('ok')]}"
        )

    def test_complete_install_every_check_ok(self, orch, tmp_path):
        root = _build_full_install(tmp_path)
        _, records, _ = _run(orch, root)
        bad = [r for r in records if not r.get("ok")]
        assert not bad, f"unexpected failing checks: {bad}"

    def test_emits_at_least_one_check_per_routine(self, orch, tmp_path):
        """Each routine in config must produce at least one check —
        otherwise a new routine could land without verification."""
        cfg = _baseline_config(routines=[
            {
                "id": "first-routine", "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "0 9 * * *", "human": "9 AM"},
                "purpose": "first.", "iter_added": 1,
            },
            {
                "id": "second-routine", "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "0 18 * * *", "human": "6 PM"},
                "purpose": "second.", "iter_added": 1,
            },
        ])
        root = _build_full_install(tmp_path, config=cfg)
        _, records, _ = _run(orch, root)
        # Each routine should produce a `routine-skill:<id>` check.
        rids = {r["check"] for r in records if r["check"].startswith("routine-skill:")}
        assert "routine-skill:first-routine" in rids
        assert "routine-skill:second-routine" in rids


# ---------------------------------------------------------------------------
# Each missing artifact produces a failing check
# ---------------------------------------------------------------------------


class TestMissingArtifacts:
    def test_missing_config_yaml_fails(self, orch, tmp_path):
        """Empty repo (no .iteration/config.yaml). The config check
        must fail loudly — without config, nothing else is meaningful."""
        rc, records, _ = _run(orch, tmp_path)
        assert rc != 0
        config_check = _check(records, "config-yaml")
        assert not config_check["ok"]
        assert "config.yaml" in config_check["detail"].lower()

    def test_missing_preamble_fails(self, orch, tmp_path):
        """preamble.md is the shared contract every routine SKILL.md
        references. Missing it = routines fire but have no rules."""
        root = _build_full_install(tmp_path)
        (root / ".claude" / "skills" / "_shared" / "preamble.md").unlink()
        rc, records, _ = _run(orch, root)
        assert rc != 0
        check = _check(records, "preamble")
        assert not check["ok"]
        assert "preamble" in check["detail"].lower()

    def test_missing_per_routine_skill_fails(self, orch, tmp_path):
        """config lists a routine but .claude/skills/<id>/SKILL.md
        is absent. Without it, the slash command `/<routine_id>`
        won't resolve."""
        root = _build_full_install(tmp_path)
        (root / ".claude" / "skills" / "prd-implement" / "SKILL.md").unlink()
        rc, records, _ = _run(orch, root)
        assert rc != 0
        check = _check(records, "routine-skill:prd-implement")
        assert not check["ok"]


# ---------------------------------------------------------------------------
# Placeholder leak detection — the original PRD failure mode
# ---------------------------------------------------------------------------


class TestPlaceholderLeak:
    def test_skill_md_with_leftover_placeholder_fails(self, orch, tmp_path):
        """The exact failure mode the wrapper from PR #57 was built to
        eliminate. If a rendered SKILL.md ships with `{{routine_id}}`
        still inside, install-doctor MUST catch it — otherwise the
        full integration test would silently pass on a broken install."""
        root = _build_full_install(
            tmp_path,
            routine_skill_body="# {{routine_id}}\n\nstill broken.\n",
        )
        rc, records, _ = _run(orch, root)
        assert rc != 0, (
            "rendered SKILL.md with `{{routine_id}}` left in must fail "
            "install-doctor — this is the whole point of the audit"
        )
        check = _check(records, "routine-skill:prd-implement")
        assert not check["ok"]
        assert "placeholder" in check["detail"].lower() or "{{" in check["detail"]


# ---------------------------------------------------------------------------
# post-commit hook — only required when a git-hook routine exists
# ---------------------------------------------------------------------------


class TestPostCommitHook:
    def test_git_hook_routine_requires_executable_post_commit(self, orch, tmp_path):
        """If any routine has `primitive: git-hook`, the post-commit
        hook MUST exist and be executable. A non-executable hook is
        worse than a missing one — git would happily skip it."""
        cfg = _baseline_config(routines=[
            {
                "id": "commit-tests", "state": "ACTIVE",
                "primitive": "git-hook",
                "trigger": {"human": "on every commit"},
                "purpose": "Run tests after every commit.",
                "iter_added": 1,
            },
        ])
        root = _build_full_install(
            tmp_path, config=cfg, post_commit_executable=False,
        )
        rc, records, _ = _run(orch, root)
        assert rc != 0
        check = _check(records, "post-commit-hook")
        assert not check["ok"]
        assert "exec" in check["detail"].lower()

    def test_git_hook_routine_missing_post_commit_fails(self, orch, tmp_path):
        cfg = _baseline_config(routines=[
            {
                "id": "commit-tests", "state": "ACTIVE",
                "primitive": "git-hook",
                "trigger": {"human": "on every commit"},
                "purpose": "Run tests after every commit.",
                "iter_added": 1,
            },
        ])
        root = _build_full_install(
            tmp_path, config=cfg, create_post_commit=False,
        )
        rc, records, _ = _run(orch, root)
        assert rc != 0
        check = _check(records, "post-commit-hook")
        assert not check["ok"]

    def test_no_git_hook_routine_skips_post_commit_check(self, orch, tmp_path):
        """If only `scheduled` / `pr-poll` routines exist, the
        post-commit check shouldn't fail — there's nothing for it to
        dispatch. (We DO still emit the check, but with `ok: true`
        and an `n/a` detail — auditing transparency.)"""
        # Default baseline is scheduled-only; intentionally don't
        # create a post-commit file.
        root = _build_full_install(tmp_path, create_post_commit=False)
        rc, records, _ = _run(orch, root)
        assert rc == 0, (
            f"scheduled-only install with no post-commit should pass; "
            f"failing: {[r for r in records if not r.get('ok')]}"
        )
        check = _check(records, "post-commit-hook")
        assert check["ok"], (
            "post-commit-hook check should be ok when no git-hook "
            "routine exists (with 'n/a' detail)"
        )


# ---------------------------------------------------------------------------
# Output shape — JSONL parseability + exit code contract
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_every_emitted_line_parses_as_json(self, orch, tmp_path):
        """Callers (CI dashboards, the future status command) parse
        the output as JSONL. A single non-JSON line breaks all of them."""
        root = _build_full_install(tmp_path)
        rc, records, _ = _run(orch, root)
        # _parse_jsonl already does json.loads — if we got records,
        # they all parsed. Also assert non-empty.
        assert records, "install-doctor must emit at least one check record"
        for r in records:
            assert "check" in r and "ok" in r and "detail" in r, (
                f"record missing canonical fields: {r}"
            )
            assert isinstance(r["ok"], bool)

    def test_exit_code_1_when_any_check_fails(self, orch, tmp_path):
        """Empty repo → multiple failing checks → exit 1. The exit
        code is the only thing CI uses to gate merges; if it's 0
        on a broken install, the gate is useless."""
        rc, _, _ = _run(orch, tmp_path)
        assert rc == 1

    def test_repo_root_required_argument(self, orch, tmp_path):
        """argparse must reject a missing --repo-root — running
        install-doctor against the current working directory by
        accident would audit the WRONG repo."""
        out = io.StringIO()
        err = io.StringIO()
        rc = orch.cli_main(["install-doctor"], stdout=out, stderr=err)
        assert rc != 0, "argparse must reject missing --repo-root"


# ---------------------------------------------------------------------------
# SKILL.md `Mode: doctor` drift — the slash command must dispatch to the wrapper
# ---------------------------------------------------------------------------


class TestModeDoctorWiring:
    """The subcommand from this slice is only useful if a user can
    invoke it. SKILL.md must expose `/auto-routines doctor` as a
    Mode that dispatches to `install-doctor`. These pins are drift
    detectors — if someone moves the Mode or changes its name, the
    install procedure ships an unreachable wrapper."""

    SKILL_MD = ROOT / "SKILL.md"

    def _doctor_mode_block(self) -> str:
        """Return just the `## Mode: doctor` block — bounded by its
        own header and the next `## Mode:` header — so a
        `install-doctor` mention in some other section doesn't
        accidentally satisfy the pin."""
        import re
        text = self.SKILL_MD.read_text()
        m = re.search(r"^## Mode: `?doctor`?\s*$", text, re.M)
        assert m, (
            "SKILL.md must expose a `## Mode: doctor` section — "
            "without it, the `install-doctor` subcommand is "
            "unreachable from the user-facing slash command surface"
        )
        start = m.start()
        nxt = re.search(r"^## Mode: ", text[m.end():], re.M)
        end = m.end() + nxt.start() if nxt else len(text)
        return text[start:end]

    def test_mode_doctor_section_exists(self):
        # Side-effect: _doctor_mode_block asserts existence.
        block = self._doctor_mode_block()
        assert block, "Mode: doctor block is empty"

    def test_mode_doctor_invokes_install_doctor_subcommand(self):
        block = self._doctor_mode_block()
        assert "install-doctor" in block, (
            "`Mode: doctor` block must invoke "
            "`scripts/orchestrator.py install-doctor` — the wrapper "
            "this Mode dispatches to. Without the subcommand name, "
            "the LLM has nothing to run."
        )

    def test_mode_doctor_passes_repo_root(self):
        """The wrapper REQUIRES --repo-root (no cwd fallback).
        SKILL.md must show the user how to pass it — otherwise the
        first invocation crashes with an argparse error."""
        block = self._doctor_mode_block()
        assert "--repo-root" in block, (
            "`Mode: doctor` must show `--repo-root` in its invocation "
            "— the wrapper has no cwd default, so an invocation "
            "without this flag fails at argparse"
        )

    def test_mode_doctor_declares_pure_script_no_llm(self):
        """Token-frugality discipline (PRD: 'Token frugality'): every
        deterministic wrapper Mode must declare 'does not spawn an LLM'
        so the install procedure's expected behavior is unambiguous.
        Mirrors `Mode: status` / `Mode: test-fire` / `Mode: budget`
        prose discipline."""
        block = self._doctor_mode_block()
        # Tolerate phrasing variations — just look for the canonical
        # signal that LLM tokens aren't spent.
        normalized = block.lower()
        assert (
            "does not spawn an llm" in normalized
            or "no claude tokens" in normalized
            or "pure-script" in normalized
        ), (
            "`Mode: doctor` must declare it does not spawn an LLM — "
            "consistent with `Mode: status` / `Mode: test-fire` / "
            "`Mode: budget`. Without this declaration, callers don't "
            "know if invoking `/auto-routines doctor` costs tokens."
        )
