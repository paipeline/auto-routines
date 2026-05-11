"""
Drift detectors for `orchestrator.py cadence <routine_id> <cron>` —
issue #83 (PRD #74).

The cadence subcommand is the per-routine slider that's been a UX
gap. Today the only retuning paths are `budget low|medium|high`
(bulk re-apply, all routines) or manual YAML editing — neither
fits the "this one routine is firing too often" use case.

Behavior pinned here:

1. Validates the routine id exists in config.yaml — unknown id
   fails with rc=1 and lists valid ids.
2. Validates the cron string is well-formed — bad cron fails
   rc=1 with a clear error.
3. Validates the cron respects the current budget tier's daily-
   fire cap. Tier caps (chosen to make the existing budget
   presets fit):
     low    ≤ 1   fire/day
     medium ≤ 4   fires/day
     high   ≤ 24  fires/day
     custom unlimited
   When the cap is exceeded the error must point the user to
   `budget` so they know how to lift the cap.
4. On success, rewrites `routines[i].trigger.{cron,human}`
   atomically (tempfile + os.replace via `_atomic_write_yaml`).
5. Emits an `mcp-plan:` block with one JSON line per touched
   routine that carries a stored `task_id` — mirrors the budget
   command shape so the SKILL.md `Mode: cadence` block can
   dispatch the MCP update the same way.
6. Pure-script: no LLM, no network. Pinned by an indirect
   contract (no subprocess except for git which is not used here).
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


def _base_config() -> dict:
    return {
        "schema_version": 4,
        "repo_slug": "demo",
        "goal": "ship v1",
        "mode": "goal-driven",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": [
            {
                "id": "prd-implement",
                "primitive": "scheduled",
                "trigger": {"cron": "0 */4 * * *", "human": "every 4 hours"},
                "purpose": "drive PRD",
                "automation_level": "auto",
                "state": "ACTIVE",
                "self_evolve": True,
                "stagnation_threshold": 5,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
                "task_id": "auto-routines-demo-prd-implement",
            },
            {
                "id": "daily-digest",
                "primitive": "scheduled",
                "trigger": {"cron": "0 18 * * *", "human": "6:00 PM daily"},
                "purpose": "summarize the day",
                "automation_level": "auto",
                "state": "ACTIVE",
                "self_evolve": False,
                "stagnation_threshold": 14,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
                "task_id": "auto-routines-demo-daily-digest",
            },
            {
                "id": "commit-tests",
                "primitive": "git-hook",
                "trigger": {"event": "post-commit"},
                "purpose": "run tests after commit",
                "automation_level": "auto",
                "state": "ACTIVE",
                "self_evolve": False,
                "stagnation_threshold": 30,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 3,
            "default_stagnation_threshold": 5,
            "budget": "high",
        },
    }


def _write_config(tmp_path: Path, config: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(config, sort_keys=False))
    return p


# ---------------------------------------------------------------------------
# cron parser / fires-per-day helper
# ---------------------------------------------------------------------------

class TestCronFiresPerDay:
    """`orchestrator.cron_fires_per_day(cron) -> float` — average fires
    over a 7-day window. Used by the budget-cap check; pin it
    independently so we don't ship a cap that silently miscounts."""

    def test_every_minute(self, orch):
        assert orch.cron_fires_per_day("* * * * *") == 1440.0

    def test_every_hour(self, orch):
        assert orch.cron_fires_per_day("0 * * * *") == 24.0

    def test_every_4_hours(self, orch):
        assert orch.cron_fires_per_day("0 */4 * * *") == 6.0

    def test_daily(self, orch):
        assert orch.cron_fires_per_day("0 18 * * *") == 1.0

    def test_weekdays(self, orch):
        # Mon-Fri at 9 AM. 5/week = 5/7 ≈ 0.714.
        result = orch.cron_fires_per_day("0 9 * * 1-5")
        assert 0.7 < result < 0.72

    def test_weekly(self, orch):
        # One day per week → 1/7 ≈ 0.143.
        result = orch.cron_fires_per_day("0 9 * * 1")
        assert 0.14 < result < 0.15

    def test_rejects_wrong_field_count(self, orch):
        with pytest.raises(ValueError):
            orch.cron_fires_per_day("* * * *")
        with pytest.raises(ValueError):
            orch.cron_fires_per_day("* * * * * *")

    def test_rejects_garbage_field(self, orch):
        # Range with letters — not a valid cron expression.
        with pytest.raises(ValueError):
            orch.cron_fires_per_day("* * * * abc")


# ---------------------------------------------------------------------------
# cadence CLI: happy path
# ---------------------------------------------------------------------------

class TestCadenceHappyPath:
    def test_updates_routine_cron_and_human(self, orch, tmp_path):
        cfg_path = _write_config(tmp_path, _base_config())
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */6 * * *",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc == 0
        cfg = yaml.safe_load(cfg_path.read_text())
        r = next(r for r in cfg["routines"] if r["id"] == "prd-implement")
        assert r["trigger"]["cron"] == "0 */6 * * *", (
            "cadence must rewrite the routine's trigger.cron"
        )
        # human must be regenerated — leaving it stale would lie to
        # the status table.
        assert r["trigger"]["human"] != "every 4 hours", (
            "cadence must regenerate trigger.human; stale human "
            "string would lie to status table readers"
        )

    def test_emits_before_after_summary(self, orch, tmp_path):
        cfg_path = _write_config(tmp_path, _base_config())
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */6 * * *",
            ],
            stdout=out,
            stderr=io.StringIO(),
        )
        assert rc == 0
        text = out.getvalue()
        # Issue #83 acceptance: "Echo the before/after cron"
        assert "0 */4 * * *" in text, "before cron must appear"
        assert "0 */6 * * *" in text, "after cron must appear"

    def test_emits_mcp_plan_for_scheduled_routine(self, orch, tmp_path):
        """Scheduled routines have a stored `task_id` — cadence must
        emit an MCP update plan line so SKILL.md `Mode: cadence` can
        dispatch the actual MCP retune. Mirrors `budget` command."""
        cfg_path = _write_config(tmp_path, _base_config())
        out = io.StringIO()
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */6 * * *",
            ],
            stdout=out,
            stderr=io.StringIO(),
        )
        assert rc == 0
        text = out.getvalue()
        assert "mcp-plan:" in text, (
            "cadence must emit an `mcp-plan:` marker like `budget` "
            "does — without it, the SKILL.md block can't find the "
            "MCP retune payload"
        )
        # Parse the JSON line under the marker.
        plan_lines = text.split("mcp-plan:", 1)[1].strip().splitlines()
        plan = json.loads(plan_lines[0])
        assert plan["routine_id"] == "prd-implement"
        assert plan["task_id"] == "auto-routines-demo-prd-implement"
        assert plan["cron"] == "0 */6 * * *"

    def test_leaves_other_routines_byte_identical(self, orch, tmp_path):
        cfg_path = _write_config(tmp_path, _base_config())
        before_cfg = yaml.safe_load(cfg_path.read_text())
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */6 * * *",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc == 0
        after_cfg = yaml.safe_load(cfg_path.read_text())
        # Everything except prd-implement's trigger must be unchanged.
        for r_before, r_after in zip(before_cfg["routines"], after_cfg["routines"]):
            if r_before["id"] == "prd-implement":
                continue
            assert r_before == r_after, (
                f"cadence must NOT touch routine {r_before['id']!r}; "
                f"this is the per-routine slider, not a bulk re-apply"
            )


# ---------------------------------------------------------------------------
# cadence CLI: validation
# ---------------------------------------------------------------------------

class TestCadenceValidation:
    def test_unknown_routine_lists_valid_ids(self, orch, tmp_path):
        cfg_path = _write_config(tmp_path, _base_config())
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "nope",
                "--cron", "0 */6 * * *",
            ],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc == 1, "unknown routine id must fail rc=1"
        message = err.getvalue()
        # Error must enumerate valid ids so the user can fix the
        # typo without re-running.
        assert "prd-implement" in message and "daily-digest" in message, (
            f"unknown routine error must list valid ids; got: {message!r}"
        )

    def test_malformed_cron_rejected(self, orch, tmp_path):
        cfg_path = _write_config(tmp_path, _base_config())
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "not a cron",
            ],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc == 1, "malformed cron must fail rc=1"
        message = err.getvalue().lower()
        assert "cron" in message, (
            "malformed-cron error must mention the offending field "
            f"so the user knows what to fix; got: {message!r}"
        )

    def test_cron_exceeding_low_tier_cap_rejected(self, orch, tmp_path):
        cfg = _base_config()
        cfg["meta"]["budget"] = "low"  # cap: ≤ 1 fire/day
        cfg_path = _write_config(tmp_path, cfg)
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */4 * * *",  # 6/day — exceeds low's 1
            ],
            stdout=io.StringIO(),
            stderr=err,
        )
        assert rc == 1, (
            "cron exceeding the budget tier cap must fail rc=1"
        )
        message = err.getvalue().lower()
        # The error must mention `budget` so the user knows the
        # remedy is to bump the tier — not to give up.
        assert "budget" in message, (
            f"tier-cap error must point at `budget` so the user "
            f"knows how to lift the cap; got: {message!r}"
        )
        # And it must mention the tier and the offending count.
        assert "low" in message, "error must name the current tier"

    def test_cron_within_medium_tier_cap_accepted(self, orch, tmp_path):
        """Sanity: medium tier (cap 4/day) accepts every-8-hours
        (3/day) — confirms the cap isn't accidentally too tight."""
        cfg = _base_config()
        cfg["meta"]["budget"] = "medium"
        cfg_path = _write_config(tmp_path, cfg)
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */8 * * *",  # 3/day ≤ 4/day
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc == 0, (
            "every-8-hours (3/day) must fit the medium tier cap "
            "(4/day) — otherwise the cap is unusably tight"
        )

    def test_custom_tier_no_cap(self, orch, tmp_path):
        """`custom` tier is the escape hatch — even every-minute
        crons must be accepted so users can opt out of the cap."""
        cfg = _base_config()
        cfg["meta"]["budget"] = "custom"
        cfg_path = _write_config(tmp_path, cfg)
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "* * * * *",  # 1440/day
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc == 0, (
            "`custom` tier must accept any valid cron — the whole "
            "point of `custom` is to bypass the preset caps"
        )

    def test_non_scheduled_routine_emits_warning_not_plan(self, orch, tmp_path):
        """git-hook / hook / loop / pr-poll routines don't have an
        MCP task_id. cadence on them must still succeed (the cron
        field may still be useful for documentation), but the mcp-
        plan block must contain a warning rather than a JSON line.
        Silently no-op'ing on missing task_id is the failure mode
        the warning prevents — mirrors `budget` semantics."""
        cfg_path = _write_config(tmp_path, _base_config())
        out = io.StringIO()
        err = io.StringIO()
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "commit-tests",
                "--cron", "0 */6 * * *",
            ],
            stdout=out,
            stderr=err,
        )
        # commit-tests is git-hook, no task_id — but cadence still
        # accepts the override (rc=0) and surfaces a warning so the
        # user knows the live trigger isn't actually rescheduled.
        assert rc == 0, (
            "cadence on a non-scheduled routine must still succeed; "
            "the YAML override is documentation"
        )
        text = out.getvalue() + err.getvalue()
        assert "task_id" in text.lower() or "no task" in text.lower(), (
            "cadence on a routine without a task_id must surface "
            f"a warning; got: out={out.getvalue()!r} "
            f"err={err.getvalue()!r}"
        )


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_no_temp_file_left_on_disk(self, orch, tmp_path):
        cfg_path = _write_config(tmp_path, _base_config())
        rc = orch.cli_main(
            [
                "cadence",
                "--config", str(cfg_path),
                "--routine", "prd-implement",
                "--cron", "0 */6 * * *",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        assert rc == 0
        # No `.tmp` file dangling next to the config — atomic write
        # via tempfile + os.replace must clean up after itself.
        leftovers = [p.name for p in cfg_path.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == [], (
            f"atomic write must not leave temp files behind; got: {leftovers}"
        )


# ---------------------------------------------------------------------------
# SKILL.md documentation drift detector
# ---------------------------------------------------------------------------

class TestSkillMdDocsCadence:
    """The issue #83 acceptance criterion requires SKILL.md to
    document the new mode. Pin both directions so doc and code
    can't drift apart."""

    SKILL_PATH = ROOT / "SKILL.md"

    def test_skill_md_mentions_cadence_subcommand(self):
        text = self.SKILL_PATH.read_text()
        assert "cadence" in text.lower(), (
            "SKILL.md must mention the `cadence` subcommand "
            "(issue #83 acceptance criterion). Without it, users "
            "don't know the flag exists."
        )

    def test_skill_md_explains_budget_cap_relationship(self):
        """The error message points users at `budget` to lift the
        cap — SKILL.md must explain this relationship so the user
        understands why cadence can reject a valid cron."""
        text = self.SKILL_PATH.read_text()
        # Look for a passage that mentions both `cadence` and
        # `budget` in close proximity — using the table of contents
        # or any prose paragraph.
        lower = text.lower()
        cadence_idx = lower.find("cadence")
        # Find the nearest `budget` mention within 400 chars.
        if cadence_idx == -1:
            pytest.skip("covered by test_skill_md_mentions_cadence_subcommand")
        window = text[max(0, cadence_idx - 400): cadence_idx + 400].lower()
        assert "budget" in window, (
            "SKILL.md must explain the cadence ↔ budget relationship "
            "near the cadence mention; the cap-exceeded error tells "
            "users to use `budget`, so the doc must close the loop"
        )
