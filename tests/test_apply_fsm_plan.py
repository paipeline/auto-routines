"""
Tests for `scripts/orchestrator.py apply-fsm-plan` — the deterministic
write-half of the evolve flow.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Add tests for the `evolve` flow — drain `evolve_requests.jsonl`,
    perform the FSM transitions, write a checkpoint, apply, verify."

This is the **apply** half (the last two halves were drain ✓ + fsm-plan
✓ + checkpoint-append ✓). It consumes plan lines (the JSONL emitted by
`fsm-plan`) and rewrites `config.yaml` atomically so routines[i].state
matches the plan's `to` value. Pre-flight check is all-or-nothing: a
single invalid transition aborts the whole plan; config.yaml is never
left half-applied.

Why a pure-script wrapper: the apply step was prose in SKILL.md
`Mode: evolve` step 5, asking the LLM to "edit the config.yaml to
update each routine's state." That's exactly the kind of fragile
manual-edit-then-pray we want out of the model — the inputs are
structured (JSONL plan) and the output is structured (YAML mutation),
so it deserves a deterministic wrapper that's mocked in CI, not a
runtime LLM call.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml


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
# Fixture builders — build a minimal valid config.yaml in `tmp_path`
# ---------------------------------------------------------------------------


def _make_routine(rid: str, state: str = "ACTIVE") -> dict:
    """A minimal schema-4 routine record. apply-fsm-plan only cares
    about `id` and `state`; everything else is filler so the YAML
    round-trips cleanly."""
    return {
        "id": rid,
        "state": state,
        "primitive": "scheduled",
        "execution_surface": "local",
        "trigger": {"cron": "0 9 * * *", "human": "9:00 AM daily"},
        "purpose": f"Test routine {rid}.",
        "success_criterion": "n/a (test fixture)",
        "iter_added": 1,
        "self_evolve": False,
        "automation_level": "auto",
    }


def _write_config(path: Path, routines: list[dict]) -> None:
    cfg = {
        "schema_version": 4,
        "repo_slug": "tmp-test-repo",
        "goal": "Test config",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def _write_plan(path: Path, entries: list[dict]) -> None:
    """JSONL — one plan record per line, matching fsm-plan's output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _run(orch, *argv, stdin: str | None = None):
    """Invoke `orchestrator.cli_main` with captured stdout/stderr.
    Returns (rc, stdout_str, stderr_str, records).

    `records` is the list of parsed JSONL output lines (skipping blank
    and comment lines)."""
    out = io.StringIO()
    err = io.StringIO()
    # apply-fsm-plan reads from stdin when --plan is `-`. We splice
    # sys.stdin temporarily so the CLI under test sees our payload.
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        rc = orch.cli_main(list(argv), stdout=out, stderr=err)
    finally:
        sys.stdin = old_stdin
    raw = out.getvalue()
    records = []
    for line in raw.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Not all output is JSON (some commands print summary lines);
                # apply-fsm-plan should be pure JSONL though.
                pass
    return rc, raw, err.getvalue(), records


# ---------------------------------------------------------------------------
# Happy path: valid plan, every transition lands
# ---------------------------------------------------------------------------


class TestApplyHappyPath:
    def test_single_transition_writes_state(self, orch, tmp_path):
        """The canonical case: one ACTIVE→STAGNANT transition. After
        apply, the config.yaml on disk has the new state."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT",
             "reason": "7 runs since last useful (threshold=7)"},
        ])

        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc == 0

        new_cfg = yaml.safe_load(cfg_path.read_text())
        states = {r["id"]: r["state"] for r in new_cfg["routines"]}
        assert states == {"alpha": "STAGNANT"}

    def test_multiple_transitions_all_applied(self, orch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [
            _make_routine("alpha", "ACTIVE"),
            _make_routine("bravo", "ACTIVE"),
            _make_routine("charlie", "ACTIVE"),
        ])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
            {"routine_id": "bravo", "from": "ACTIVE", "to": "EVOLVING"},
        ])

        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc == 0

        new_cfg = yaml.safe_load(cfg_path.read_text())
        states = {r["id"]: r["state"] for r in new_cfg["routines"]}
        assert states == {
            "alpha": "STAGNANT",
            "bravo": "EVOLVING",
            "charlie": "ACTIVE",  # not in the plan, untouched
        }

    def test_emits_ok_record_per_transition(self, orch, tmp_path):
        """JSONL output: one record per transition, with `ok: true`."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [
            _make_routine("alpha", "ACTIVE"),
            _make_routine("bravo", "ACTIVE"),
        ])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
            {"routine_id": "bravo", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        rc, _raw, _err, records = _run(orch, "apply-fsm-plan",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc == 0
        assert len(records) == 2
        for r in records:
            assert r["ok"] is True
            assert r["from"] == "ACTIVE"
            assert r["to"] == "STAGNANT"
        ids = {r["routine_id"] for r in records}
        assert ids == {"alpha", "bravo"}

    def test_other_routines_untouched_including_other_fields(self, orch, tmp_path):
        """We mutate ONLY the `state` field — purpose, trigger, stats,
        etc. round-trip unchanged. (A test for the round-trip would
        catch a bug where we accidentally reconstructed the routine
        dict from scratch instead of mutating it in place.)"""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        original = _make_routine("alpha", "ACTIVE")
        original["purpose"] = "Carefully crafted purpose string"
        original["trigger"]["cron"] = "5 14 * * 1-5"
        _write_config(cfg_path, [original])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        _run(orch, "apply-fsm-plan",
             "--config", str(cfg_path), "--plan", str(plan_path))

        new_cfg = yaml.safe_load(cfg_path.read_text())
        r = new_cfg["routines"][0]
        assert r["state"] == "STAGNANT"
        assert r["purpose"] == "Carefully crafted purpose string"
        assert r["trigger"]["cron"] == "5 14 * * 1-5"

    def test_empty_plan_is_noop(self, orch, tmp_path):
        """An empty plan (e.g. fsm-plan produced no stagnant routines)
        must be a clean no-op — exit 0, config unchanged."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("")  # truly empty

        before = cfg_path.read_text()
        rc, _raw, _err, records = _run(orch, "apply-fsm-plan",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc == 0
        assert records == []
        assert cfg_path.read_text() == before  # byte-identical


# ---------------------------------------------------------------------------
# Atomicity: a single invalid line aborts the whole plan
# ---------------------------------------------------------------------------


class TestApplyAtomic:
    def test_one_invalid_transition_blocks_all(self, orch, tmp_path):
        """If ANY plan line fails the precondition (wrong `from`,
        unknown routine, etc.), the rewrite is aborted — config.yaml
        must be byte-identical to its pre-apply state. Half-applied
        configs are the worst-case failure mode."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [
            _make_routine("alpha", "ACTIVE"),
            _make_routine("bravo", "ACTIVE"),
        ])
        # First line valid; second references a routine that doesn't exist.
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
            {"routine_id": "ghost", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        before = cfg_path.read_text()
        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc != 0
        # CRITICAL: alpha is NOT advanced even though its line was valid.
        # The whole plan rolls back.
        assert cfg_path.read_text() == before

    def test_no_tmp_file_left_on_success(self, orch, tmp_path):
        """Atomic write via tempfile+os.replace must clean up after
        itself — no stale `.tmp` or `.yaml.tmp` siblings."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        _run(orch, "apply-fsm-plan",
             "--config", str(cfg_path), "--plan", str(plan_path))

        # No .tmp leftovers anywhere in the config dir.
        siblings = [p.name for p in cfg_path.parent.iterdir()]
        leftover = [s for s in siblings if s.endswith(".tmp")]
        assert leftover == [], (
            f"atomic write must clean up .tmp files; found: {leftover}"
        )


# ---------------------------------------------------------------------------
# Validation: malformed plan, mismatched `from`, missing routine
# ---------------------------------------------------------------------------


class TestApplyValidation:
    def test_routine_not_found_fails(self, orch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        _write_plan(plan_path, [
            {"routine_id": "ghost", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        rc, _raw, err_text, records = _run(orch, "apply-fsm-plan",
                                           "--config", str(cfg_path),
                                           "--plan", str(plan_path))
        assert rc != 0
        # The failure surfaces in the JSONL output (not just stderr) so
        # an automation can branch on it.
        assert any(
            r["routine_id"] == "ghost" and r["ok"] is False
            for r in records
        ), f"missing routine must emit ok:false; got: {records}"

    def test_from_state_mismatch_fails(self, orch, tmp_path):
        """Plan says routine is ACTIVE but it's actually STAGNANT.
        Refusing to apply means a stale plan can't accidentally pause
        a routine that was just reactivated."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "STAGNANT")])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        before = cfg_path.read_text()
        rc, _raw, _err, records = _run(orch, "apply-fsm-plan",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc != 0
        assert cfg_path.read_text() == before
        bad = [r for r in records if r["ok"] is False]
        assert bad, f"from-mismatch must emit ok:false; got: {records}"
        # The detail should mention the actual current state for
        # debuggability — otherwise the LLM gets stuck guessing why
        # the apply failed.
        assert "STAGNANT" in bad[0]["detail"]

    def test_malformed_json_line_fails(self, orch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text('{"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"}\nnot json at all\n')

        before = cfg_path.read_text()
        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc != 0
        assert cfg_path.read_text() == before

    def test_missing_required_field_fails(self, orch, tmp_path):
        """A plan line missing `routine_id` / `from` / `to` is invalid.
        We refuse rather than guess the missing field."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        # Missing `to`.
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE"},
        ])

        before = cfg_path.read_text()
        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc != 0
        assert cfg_path.read_text() == before

    def test_skip_blank_and_comment_lines(self, orch, tmp_path):
        """JSONL conventionally tolerates blank lines and leading-`#`
        comments — fsm-plan doesn't emit them but a hand-edited plan
        might. Skipping them is friendlier than failing."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            "# generated by fsm-plan\n"
            "\n"
            '{"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"}\n'
            "\n"
        )

        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc == 0
        new_cfg = yaml.safe_load(cfg_path.read_text())
        assert new_cfg["routines"][0]["state"] == "STAGNANT"


# ---------------------------------------------------------------------------
# Stdin: --plan - reads the plan from stdin (for piping)
# ---------------------------------------------------------------------------


class TestApplyStdin:
    def test_plan_dash_reads_stdin(self, orch, tmp_path):
        """The classic Unix pipeline:
            orchestrator.py fsm-plan ... | orchestrator.py apply-fsm-plan ... --plan -
        Wiring `--plan -` to stdin means SKILL.md `Mode: evolve` can
        chain them without an intermediate tempfile."""
        cfg_path = tmp_path / "config.yaml"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        plan_jsonl = json.dumps({
            "routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT",
        }) + "\n"

        rc, *_ = _run(orch, "apply-fsm-plan",
                      "--config", str(cfg_path),
                      "--plan", "-",
                      stdin=plan_jsonl)
        assert rc == 0
        new_cfg = yaml.safe_load(cfg_path.read_text())
        assert new_cfg["routines"][0]["state"] == "STAGNANT"


# ---------------------------------------------------------------------------
# CLI wiring: argparse parses, dispatch lands
# ---------------------------------------------------------------------------


class TestApplyCli:
    def test_unknown_command_returns_2(self, orch, tmp_path):
        """Sanity check that argparse error path still works — apply-fsm-plan
        shouldn't accidentally swallow other commands' errors."""
        rc, *_ = _run(orch, "apply-fsm-plan-typo", "--config", str(tmp_path))
        assert rc != 0

    def test_config_missing_fails_loudly(self, orch, tmp_path):
        """A missing config.yaml is operator error — exit non-zero
        with a stderr message, don't silently no-op."""
        plan_path = tmp_path / "plan.jsonl"
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])
        rc, _raw, err_text, _records = _run(
            orch, "apply-fsm-plan",
            "--config", str(tmp_path / "does-not-exist.yaml"),
            "--plan", str(plan_path),
        )
        assert rc != 0
        assert err_text.strip(), "expected an error message on stderr"
