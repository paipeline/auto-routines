"""
Tests for `scripts/orchestrator.py verify-fsm-state` — the read-side
verify-half of the evolve flow.

PRD `.iteration/goal.md` (Coverage and correctness):
    "Add tests for the `evolve` flow — drain `evolve_requests.jsonl`,
    perform the FSM transitions, write a checkpoint, apply, verify."

This completes the four-stage pipeline:

    drain-evolve-requests  →  fsm-plan  →  apply-fsm-plan  →  verify-fsm-state

Symmetric design: `verify-fsm-state` consumes the SAME JSONL plan that
`apply-fsm-plan` consumed; it just reads `to` as the **expected**
current state and asserts the config matches. So a typical evolve
sequence is:

    fsm-plan ... > plan.jsonl
    apply-fsm-plan --plan plan.jsonl --config ...
    verify-fsm-state --plan plan.jsonl --config ...   # round-trip check

The output shape mirrors `apply-fsm-plan` and `install-doctor`:
one JSON object per assertion with `{routine_id, expected, actual,
ok, detail}`. Exit 0 iff every assertion holds.

Why a pure-script wrapper: the verify step was prose in SKILL.md
`Mode: evolve` step 6 ("read the config back and check each state
changed"). The natural-language version of "read and check" inevitably
drifts — we want one read-side wrapper that's mocked in CI rather
than re-invented every fire.
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
# Fixture builders
# ---------------------------------------------------------------------------


def _make_routine(rid: str, state: str = "ACTIVE") -> dict:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _run(orch, *argv, stdin: str | None = None):
    out = io.StringIO()
    err = io.StringIO()
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
                pass
    return rc, raw, err.getvalue(), records


# ---------------------------------------------------------------------------
# Happy path: every assertion in the plan matches reality
# ---------------------------------------------------------------------------


class TestVerifyHappyPath:
    def test_state_matches_plan_to(self, orch, tmp_path):
        """The canonical case: post-apply, config alpha:STAGNANT and
        the plan we just applied said to:STAGNANT. Verify passes."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "STAGNANT")])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc == 0
        assert len(records) == 1
        assert records[0]["ok"] is True
        assert records[0]["routine_id"] == "alpha"
        assert records[0]["expected"] == "STAGNANT"
        assert records[0]["actual"] == "STAGNANT"

    def test_multiple_assertions_all_match(self, orch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [
            _make_routine("alpha", "STAGNANT"),
            _make_routine("bravo", "EVOLVING"),
            _make_routine("charlie", "ACTIVE"),
        ])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
            {"routine_id": "bravo", "from": "ACTIVE", "to": "EVOLVING"},
        ])

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc == 0
        assert len(records) == 2
        assert all(r["ok"] for r in records)

    def test_empty_plan_is_ok_no_records(self, orch, tmp_path):
        """An empty plan means "verify nothing" — that's vacuously
        true. Exit 0, emit zero records. The alternative (exit 1 on
        empty input) would force callers to special-case the
        no-transitions branch."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("")

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc == 0
        assert records == []


# ---------------------------------------------------------------------------
# Mismatch: config doesn't match the expected post-apply state
# ---------------------------------------------------------------------------


class TestVerifyMismatch:
    def test_state_mismatch_fails(self, orch, tmp_path):
        """Plan said to:STAGNANT but config shows ACTIVE — the apply
        either didn't run or didn't land. Verify catches it."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        # NB: state is still ACTIVE — the apply didn't happen.
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc != 0
        assert records[0]["ok"] is False
        assert records[0]["expected"] == "STAGNANT"
        assert records[0]["actual"] == "ACTIVE"
        # The detail must mention both states for debuggability.
        detail = records[0]["detail"].upper()
        assert "STAGNANT" in detail
        assert "ACTIVE" in detail

    def test_routine_not_found_fails(self, orch, tmp_path):
        """Plan references a routine that no longer exists in config
        (someone hand-edited it between apply and verify, perhaps).
        Surface the missing routine rather than silently passing."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        _write_plan(plan_path, [
            {"routine_id": "ghost", "from": "ACTIVE", "to": "STAGNANT"},
        ])

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc != 0
        bad = [r for r in records if not r["ok"]]
        assert any(r["routine_id"] == "ghost" for r in bad)

    def test_one_mismatch_exits_nonzero_even_with_other_passes(self, orch, tmp_path):
        """Mixed plan: one assertion matches, one doesn't. The
        passing record must still be emitted (so the user sees the
        full picture), but the overall exit code must be non-zero."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [
            _make_routine("alpha", "STAGNANT"),   # would-match
            _make_routine("bravo", "ACTIVE"),      # would-mismatch
        ])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
            {"routine_id": "bravo", "from": "ACTIVE", "to": "EVOLVING"},
        ])

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc != 0
        by_id = {r["routine_id"]: r for r in records}
        assert by_id["alpha"]["ok"] is True
        assert by_id["bravo"]["ok"] is False


# ---------------------------------------------------------------------------
# Validation: malformed plan, missing required fields
# ---------------------------------------------------------------------------


class TestVerifyValidation:
    def test_malformed_json_line_fails(self, orch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            '{"routine_id": "alpha", "from": "ACTIVE", "to": "ACTIVE"}\n'
            'not json\n'
        )

        rc, *_ = _run(orch, "verify-fsm-state",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc != 0

    def test_missing_required_field_fails(self, orch, tmp_path):
        """A plan line missing `to` can't be verified — refuse rather
        than guess. (For verify, we only strictly need `routine_id`
        and `to`; `from` is informational and ignored.)"""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "ACTIVE")])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE"},  # missing `to`
        ])

        rc, *_ = _run(orch, "verify-fsm-state",
                      "--config", str(cfg_path),
                      "--plan", str(plan_path))
        assert rc != 0

    def test_skip_blank_and_comment_lines(self, orch, tmp_path):
        """JSONL convention — match apply-fsm-plan's tolerance for
        blank lines and `#`-prefixed comments."""
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [_make_routine("alpha", "STAGNANT")])
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            "# generated by fsm-plan\n"
            "\n"
            '{"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"}\n'
            "\n"
        )

        rc, _raw, _err, records = _run(orch, "verify-fsm-state",
                                       "--config", str(cfg_path),
                                       "--plan", str(plan_path))
        assert rc == 0
        assert len(records) == 1
        assert records[0]["ok"] is True


# ---------------------------------------------------------------------------
# Stdin: --plan - reads from stdin (for piping into verify)
# ---------------------------------------------------------------------------


class TestVerifyStdin:
    def test_plan_dash_reads_stdin(self, orch, tmp_path):
        """Pipe-style usage:
            apply-fsm-plan ... && fsm-plan ... | verify-fsm-state ...
        Matches the apply-fsm-plan interface for symmetry."""
        cfg_path = tmp_path / "config.yaml"
        _write_config(cfg_path, [_make_routine("alpha", "STAGNANT")])
        plan_jsonl = json.dumps({
            "routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT",
        }) + "\n"

        rc, *_ = _run(orch, "verify-fsm-state",
                      "--config", str(cfg_path),
                      "--plan", "-",
                      stdin=plan_jsonl)
        assert rc == 0


# ---------------------------------------------------------------------------
# Round-trip: apply then verify with the SAME plan must succeed
# ---------------------------------------------------------------------------


class TestApplyVerifyRoundtrip:
    """The whole point of the verify wrapper: prove that the apply
    step actually did what it claimed. Run apply, then verify with
    the same plan — both succeed."""

    def test_apply_then_verify_with_same_plan(self, orch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        plan_path = tmp_path / "plan.jsonl"
        _write_config(cfg_path, [
            _make_routine("alpha", "ACTIVE"),
            _make_routine("bravo", "ACTIVE"),
        ])
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
            {"routine_id": "bravo", "from": "ACTIVE", "to": "EVOLVING"},
        ])

        rc_apply, *_ = _run(orch, "apply-fsm-plan",
                            "--config", str(cfg_path),
                            "--plan", str(plan_path))
        assert rc_apply == 0

        rc_verify, _raw, _err, records = _run(orch, "verify-fsm-state",
                                              "--config", str(cfg_path),
                                              "--plan", str(plan_path))
        assert rc_verify == 0
        assert all(r["ok"] for r in records)
        assert len(records) == 2


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestVerifyCli:
    def test_config_missing_fails_loudly(self, orch, tmp_path):
        plan_path = tmp_path / "plan.jsonl"
        _write_plan(plan_path, [
            {"routine_id": "alpha", "from": "ACTIVE", "to": "STAGNANT"},
        ])
        rc, _raw, err_text, _records = _run(
            orch, "verify-fsm-state",
            "--config", str(tmp_path / "does-not-exist.yaml"),
            "--plan", str(plan_path),
        )
        assert rc != 0
        assert err_text.strip(), "expected an error message on stderr"
