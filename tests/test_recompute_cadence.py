"""
Tests for value-based cadence recompute — issue #77 (PRD #74).

A routine's cron is set at install time and then sits there. If a
routine produces signal every fire, the cadence is too slow; if it
fires 10x/week and produces nothing, the cadence is too fast (and
the user pays for those minutes anyway). `recompute_cadence` reads
each routine's recent log entries and dampens / amplifies cron
within its budget tier:

  value_rate = useful_fires / total_fires
             where useful = (outcome == 'ok' AND increment_signal)

  cron = CADENCE_LADDERS[budget_tier][index]
         where index = round(value_rate * (len(ladder) - 1))

The ladder is ordered slowest → fastest. value_rate = 0.0 picks the
slow end; 1.0 picks the fast end; mixed picks the linearly-
interpolated middle.

Bounded by budget tier: a high-value routine on `low` budget can't
escape `low`'s fastest. Users must bump tier via `budget` command.

Tests cover the pure function (recompute_cadence) and the CLI
subcommand wrapper (`auto-routines recompute-cadence`).
"""
from __future__ import annotations

import importlib.util
import json
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
# CADENCE_LADDERS constant — pinned shape (drift detector)
# ---------------------------------------------------------------------------


class TestCadenceLaddersShape:
    """Pins the cadence ladder so a refactor can't silently change
    throttling behavior. The ladder is the deterministic core of the
    recompute formula — a typo here changes how fast every routine
    fires."""

    def test_three_user_tiers_have_ladders(self, orch):
        for tier in ("low", "medium", "high"):
            assert tier in orch.CADENCE_LADDERS, (
                f"CADENCE_LADDERS missing tier {tier!r} — recompute "
                f"can't throttle routines on this tier without a "
                f"ladder. Add one or remove the tier from BUDGET_TIERS."
            )

    def test_ladders_are_non_empty_lists(self, orch):
        for tier, ladder in orch.CADENCE_LADDERS.items():
            assert isinstance(ladder, list)
            assert len(ladder) >= 2, (
                f"ladder for tier {tier!r} has fewer than 2 entries — "
                "interpolation needs at least a slow end and a fast end"
            )

    def test_ladder_entries_are_cron_human_pairs(self, orch):
        for tier, ladder in orch.CADENCE_LADDERS.items():
            for entry in ladder:
                assert isinstance(entry, tuple) and len(entry) == 2, (
                    f"ladder entry for tier {tier!r} is {entry!r} — "
                    "expected a `(cron, human)` 2-tuple"
                )
                cron, human = entry
                assert isinstance(cron, str) and cron.count(" ") == 4, (
                    f"ladder entry {entry!r} has invalid cron {cron!r} "
                    "— expected 5 space-separated fields"
                )
                assert isinstance(human, str) and human.strip(), (
                    f"ladder entry {entry!r} has empty `human` summary"
                )


# ---------------------------------------------------------------------------
# recompute_cadence — pure function
# ---------------------------------------------------------------------------


def _make_log(entries: list[dict]) -> list[dict]:
    """Helper to build log lists; we don't care about field order or
    fields the recompute function doesn't read."""
    return entries


class TestRecomputeCadencePureFunction:
    """Core formula tests. Pure function: takes routines, log_entries,
    budget_tier; returns `{routine_id: (cron, human)}` only for routines
    whose cadence should change. Empty dict = nothing to do."""

    def test_all_useful_picks_fastest_in_tier(self, orch):
        routines = [
            {
                "id": "ci-watcher",
                "trigger": {"cron": "0 9 * * 1", "human": "Mondays 9:00 AM"},
            }
        ]
        log = _make_log(
            [
                {"routine": "ci-watcher", "outcome": "ok", "increment_signal": True}
                for _ in range(10)
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        fastest = orch.CADENCE_LADDERS["medium"][-1]
        assert out == {"ci-watcher": fastest}, (
            f"all-useful routine should pick the fast end of the medium "
            f"ladder; got {out!r}"
        )

    def test_all_noop_picks_slowest_in_tier(self, orch):
        routines = [
            {
                "id": "doc-drift",
                "trigger": {"cron": "0 */6 * * *", "human": "every 6h"},
            }
        ]
        log = _make_log(
            [
                {"routine": "doc-drift", "outcome": "noop", "increment_signal": False}
                for _ in range(10)
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        slowest = orch.CADENCE_LADDERS["medium"][0]
        assert out == {"doc-drift": slowest}

    def test_mixed_picks_interpolated_position(self, orch):
        ladder = orch.CADENCE_LADDERS["medium"]
        routines = [
            {
                "id": "r",
                "trigger": {"cron": ladder[0][0], "human": ladder[0][1]},
            }
        ]
        # 5 useful + 5 noop out of 10 → vr = 0.5 → middle of ladder.
        log = _make_log(
            [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(5)
            ]
            + [
                {"routine": "r", "outcome": "noop", "increment_signal": False}
                for _ in range(5)
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        # With a 3-rung ladder, vr=0.5 → index 1 (round(0.5*2)) → middle
        # With a 5-rung ladder, vr=0.5 → index 2 (round(0.5*4)) → middle
        expected_idx = round(0.5 * (len(ladder) - 1))
        assert out == {"r": ladder[expected_idx]}

    def test_no_change_returns_empty_dict(self, orch):
        # Idempotent: if the routine is already at the right rung, the
        # function returns no change (so the caller doesn't dirty the
        # config file).
        ladder = orch.CADENCE_LADDERS["medium"]
        target = ladder[-1]  # fast end (vr=1.0 picks this)
        routines = [
            {
                "id": "r",
                "trigger": {"cron": target[0], "human": target[1]},
            }
        ]
        log = _make_log(
            [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(10)
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        assert out == {}

    def test_empty_log_returns_empty_dict(self, orch):
        # No data → no decision. Refuse to throttle on no evidence.
        routines = [
            {
                "id": "r",
                "trigger": {"cron": "0 9 * * 1", "human": "Mondays 9:00 AM"},
            }
        ]
        out = orch.recompute_cadence(routines, [], "medium")
        assert out == {}

    def test_only_other_routines_in_log_returns_empty(self, orch):
        # Log has entries but none for the routine we're computing.
        routines = [
            {
                "id": "r",
                "trigger": {"cron": "0 9 * * 1", "human": "Mondays 9:00 AM"},
            }
        ]
        log = _make_log(
            [
                {"routine": "other", "outcome": "ok", "increment_signal": True}
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        assert out == {}

    def test_window_caps_entries_considered(self, orch):
        # Only the most-recent `window` (default 20) entries count.
        # An old streak of failures shouldn't dampen a routine that's
        # since recovered.
        routines = [
            {
                "id": "r",
                "trigger": {
                    "cron": orch.CADENCE_LADDERS["medium"][0][0],
                    "human": orch.CADENCE_LADDERS["medium"][0][1],
                },
            }
        ]
        # 50 old noops + 20 recent useful — the window cuts to last 20.
        log = _make_log(
            [
                {"routine": "r", "outcome": "noop", "increment_signal": False}
                for _ in range(50)
            ]
            + [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(20)
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium", window=20)
        assert out == {"r": orch.CADENCE_LADDERS["medium"][-1]}

    def test_useful_requires_both_ok_and_increment_signal(self, orch):
        # PRD: useful = (outcome == ok AND increment_signal == true).
        # An `ok` with `increment_signal: false` is a routine that
        # ran cleanly but produced nothing — that's noop-equivalent
        # for value-rate purposes.
        routines = [
            {
                "id": "r",
                "trigger": {"cron": "0 9 * * 1", "human": "Mondays 9:00 AM"},
            }
        ]
        log = _make_log(
            [
                # All `ok` but none incremented signal — same as all-noop.
                {"routine": "r", "outcome": "ok", "increment_signal": False}
                for _ in range(10)
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        assert out == {"r": orch.CADENCE_LADDERS["medium"][0]}

    def test_unknown_budget_tier_raises(self, orch):
        routines = [
            {
                "id": "r",
                "trigger": {"cron": "0 9 * * 1", "human": "Mondays 9:00 AM"},
            }
        ]
        with pytest.raises(ValueError) as exc:
            orch.recompute_cadence(routines, [], "absurd")
        assert "absurd" in str(exc.value)

    def test_custom_tier_skipped_no_change(self, orch):
        # `custom` is the user's hand-tuned tier. recompute respects
        # it by returning no changes — no ladder applies.
        routines = [
            {
                "id": "r",
                "trigger": {"cron": "0 9 * * 1", "human": "Mondays 9:00 AM"},
            }
        ]
        log = _make_log(
            [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(10)
            ]
        )
        out = orch.recompute_cadence(routines, log, "custom")
        assert out == {}

    def test_routine_with_no_trigger_cron_skipped(self, orch):
        # Hook/git-hook routines have no cron — recompute should
        # silently skip them, not crash.
        routines = [
            {"id": "h", "primitive": "hook", "trigger": {"event": "Stop"}}
        ]
        log = _make_log(
            [
                {"routine": "h", "outcome": "ok", "increment_signal": True}
            ]
        )
        out = orch.recompute_cadence(routines, log, "medium")
        assert out == {}


# ---------------------------------------------------------------------------
# CLI integration — `auto-routines recompute-cadence`
# ---------------------------------------------------------------------------


def _write_config(path: Path, routines: list[dict], budget: str = "medium") -> None:
    """Write a minimal but sanity-check-passing config.yaml."""
    import yaml
    data = {
        "schema_version": 4,
        "repo_slug": "test-repo",
        "goal": "ship",
        "mode": "fully-auto",
        "deps": {"gh": "optional", "mcps": []},
        "routines": routines,
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "budget": budget,
            "idle_window_tz": "UTC",
        },
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _write_log(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


class TestRecomputeCadenceCli:
    """End-to-end: the CLI reads config + log, computes, writes new
    cron back. Smoke-level integration — full formula correctness is
    pinned by the pure-function tests above."""

    def test_cli_rewrites_high_value_routine_to_fast_end(self, orch, tmp_path):
        import yaml
        cfg = tmp_path / "config.yaml"
        log = tmp_path / "log.jsonl"
        slowest = orch.CADENCE_LADDERS["medium"][0]
        _write_config(
            cfg,
            [
                {
                    "id": "r",
                    "primitive": "scheduled",
                    "trigger": {"cron": slowest[0], "human": slowest[1]},
                    "purpose": "test",
                    "automation_level": "auto",
                    "state": "ACTIVE",
                    "execution_surface": "local",
                }
            ],
        )
        _write_log(
            log,
            [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(20)
            ],
        )
        code = orch.cli_main(
            [
                "recompute-cadence",
                "--config",
                str(cfg),
                "--log",
                str(log),
            ],
            stdout=open("/dev/null", "w"),
            stderr=open("/dev/null", "w"),
        )
        assert code == 0
        new = yaml.safe_load(cfg.read_text())
        fastest = orch.CADENCE_LADDERS["medium"][-1]
        new_trigger = new["routines"][0]["trigger"]
        assert new_trigger["cron"] == fastest[0]
        assert new_trigger["human"] == fastest[1]

    def test_cli_idempotent(self, orch, tmp_path):
        # Running twice with no new entries must produce identical
        # config bytes the second time — the recompute hash hasn't
        # changed, so the writer should noop.
        cfg = tmp_path / "config.yaml"
        log = tmp_path / "log.jsonl"
        ladder = orch.CADENCE_LADDERS["medium"]
        # Already at the fast end with all-useful log → recompute
        # picks the same rung; both runs leave the file untouched.
        fastest = ladder[-1]
        _write_config(
            cfg,
            [
                {
                    "id": "r",
                    "primitive": "scheduled",
                    "trigger": {"cron": fastest[0], "human": fastest[1]},
                    "purpose": "test",
                    "automation_level": "auto",
                    "state": "ACTIVE",
                    "execution_surface": "local",
                }
            ],
        )
        _write_log(
            log,
            [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(20)
            ],
        )
        before_bytes = cfg.read_bytes()
        for _ in range(2):
            code = orch.cli_main(
                [
                    "recompute-cadence",
                    "--config",
                    str(cfg),
                    "--log",
                    str(log),
                ],
                stdout=open("/dev/null", "w"),
                stderr=open("/dev/null", "w"),
            )
            assert code == 0
        after_bytes = cfg.read_bytes()
        assert before_bytes == after_bytes, (
            "recompute-cadence must be idempotent — running twice with "
            "no log changes should leave config.yaml byte-identical"
        )

    def test_cli_emits_summary_to_stdout(self, orch, tmp_path):
        import io
        cfg = tmp_path / "config.yaml"
        log = tmp_path / "log.jsonl"
        slowest = orch.CADENCE_LADDERS["medium"][0]
        _write_config(
            cfg,
            [
                {
                    "id": "r",
                    "primitive": "scheduled",
                    "trigger": {"cron": slowest[0], "human": slowest[1]},
                    "purpose": "test",
                    "automation_level": "auto",
                    "state": "ACTIVE",
                    "execution_surface": "local",
                }
            ],
        )
        _write_log(
            log,
            [
                {"routine": "r", "outcome": "ok", "increment_signal": True}
                for _ in range(20)
            ],
        )
        out = io.StringIO()
        err = io.StringIO()
        code = orch.cli_main(
            ["recompute-cadence", "--config", str(cfg), "--log", str(log)],
            stdout=out,
            stderr=err,
        )
        assert code == 0
        # Human-readable summary line per touched routine.
        assert "r" in out.getvalue()
        assert err.getvalue() == ""
