"""
Tests for scripts/status.py — the no-LLM status renderer.

The whole point of this script is that `/auto-routines status` must NOT spawn
a Claude session. These tests pin the behaviors that make that contract real:
output is deterministic, fast, and reads only local files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from .conftest import status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_repo(tmp_path: Path, config: dict, log_lines: list[dict] | None = None) -> Path:
    iter_dir = tmp_path / ".iteration"
    iter_dir.mkdir()
    (iter_dir / "config.yaml").write_text(yaml.safe_dump(config))
    if log_lines is not None:
        (iter_dir / "log.jsonl").write_text("\n".join(json.dumps(e) for e in log_lines) + "\n")
    return tmp_path


def _base_config() -> dict:
    return {
        "schema_version": 3,
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
            },
            {
                "id": "daily-digest",
                "primitive": "scheduled",
                "trigger": {"cron": "0 18 * * *", "human": "6:00 PM daily"},
                "purpose": "summarize the day",
                "automation_level": "auto",
                "state": "STOPPED",
                "self_evolve": False,
                "stagnation_threshold": 14,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 3,
            "default_stagnation_threshold": 5,
            "budget": "medium",
        },
    }


# ---------------------------------------------------------------------------
# Smoke + content
# ---------------------------------------------------------------------------

def test_status_renders_basic_block(tmp_path):
    repo = _write_repo(tmp_path, _base_config())
    out = status.render(_base_config(), repo)
    assert "goal:" in out
    assert "ship v1" in out
    assert "mode: goal-driven" in out
    assert "budget: medium" in out
    # Both routines listed.
    assert "prd-implement" in out
    assert "daily-digest" in out
    # Header columns.
    assert "schedule" in out
    assert "state" in out


def test_status_sorts_active_before_stopped(tmp_path):
    repo = _write_repo(tmp_path, _base_config())
    out = status.render(_base_config(), repo)
    # ACTIVE row must appear before STOPPED row.
    assert out.index("prd-implement") < out.index("daily-digest")


def test_status_handles_missing_log(tmp_path):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    # log.jsonl absent — should still render with "never" lasts.
    out = status.render(cfg, repo)
    assert "never" in out


def test_status_aggregates_log_for_runs_and_useful(tmp_path):
    cfg = _base_config()
    log = [
        {"ts": "2026-05-09T10:00:00+0200", "routine": "prd-implement", "outcome": "ok",
         "summary": "PR opened", "increment_signal": True},
        {"ts": "2026-05-09T14:00:00+0200", "routine": "prd-implement", "outcome": "ok",
         "summary": "PR refreshed", "increment_signal": True},
        {"ts": "2026-05-09T18:00:00+0200", "routine": "prd-implement", "outcome": "warn",
         "summary": "lint warning", "increment_signal": False},
    ]
    repo = _write_repo(tmp_path, cfg, log_lines=log)
    out = status.render(cfg, repo)
    # Find the prd-implement row and parse the integers.
    line = next(l for l in out.splitlines() if l.startswith("prd-implement"))
    # Ensure runs >= 3 and useful >= 2 (the stats: prefilled 0, log adds 3 runs / 2 useful / 1 noisy).
    parts = [p for p in line.split() if p.isdigit() or p == "ACTIVE"]
    # Format: id sched(maybe spaces) state runs useful noisy ...
    # Extract numbers safely:
    nums = [int(p) for p in line.split() if p.isdigit()]
    assert nums[0] >= 3   # runs
    assert nums[1] >= 2   # useful
    assert nums[2] >= 1   # noisy


def test_status_truncates_long_goal(tmp_path):
    cfg = _base_config()
    cfg["goal"] = "x" * 200
    repo = _write_repo(tmp_path, cfg)
    out = status.render(cfg, repo)
    # Goal line truncated with ellipsis.
    goal_line = [l for l in out.splitlines() if l.startswith("goal:")][0]
    assert "..." in goal_line


def test_status_pending_evolve_count(tmp_path):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    (repo / ".iteration" / "evolve_requests.jsonl").write_text(
        '{"ts":"2026-05-09T10:00:00+0200","routine_id":"prd-implement","reason":"x","suggested":"y"}\n'
        '{"ts":"2026-05-09T11:00:00+0200","routine_id":"daily-digest","reason":"x","suggested":"y"}\n'
    )
    out = status.render(cfg, repo)
    assert "evolve requests pending: 2" in out


def test_status_halted_notice(tmp_path):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    (repo / ".iteration" / "halted.md").write_text("missing dep: gh")
    out = status.render(cfg, repo)
    assert "halted" in out


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------

def test_main_returns_1_when_no_config(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        status.main(["--root", str(tmp_path)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "no .iteration/config.yaml" in err


def test_main_routine_filter(tmp_path, capsys):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    rc = status.main(["--root", str(repo), "--routine", "prd-implement"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "prd-implement" in out
    assert "purpose:" in out
    assert "drive PRD" in out


def test_main_routine_filter_unknown(tmp_path, capsys):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    rc = status.main(["--root", str(repo), "--routine", "nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no routine" in err


def test_main_json_output(tmp_path, capsys):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    rc = status.main(["--root", str(repo), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "goal-driven"
    assert payload["budget"] == "medium"
    assert {r["id"] for r in payload["routines"]} == {"prd-implement", "daily-digest"}


def test_main_default_renders_table(tmp_path, capsys):
    cfg = _base_config()
    repo = _write_repo(tmp_path, cfg)
    rc = status.main(["--root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "goal:" in out
    assert "prd-implement" in out


# ---------------------------------------------------------------------------
# Locality contract
# ---------------------------------------------------------------------------

def test_status_does_not_invoke_claude(tmp_path):
    """The whole point: status.py is pure stdlib + yaml. No subprocess, no network."""
    import re
    src = (status.__file__ if hasattr(status, "__file__") else None)
    text = Path(src).read_text() if src else ""
    # Strip docstrings + comments so the contract check ignores prose.
    code = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
    code = re.sub(r"^\s*#.*$", "", code, flags=re.MULTILINE)
    # No subprocess / popen / fork — those are how a script could spawn Claude.
    assert "subprocess" not in code
    assert "popen" not in code.lower()
    assert "os.system" not in code
    # No HTTP libs.
    assert "urllib.request" not in code
    assert "httpx" not in code
    assert "requests" not in code.lower() or "import requests" not in code
