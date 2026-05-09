"""
Tests for scripts/coordinator-brief.py — the pure-shell brief generator.

The brief is what makes the coordinator agent cheap and deterministic: instead
of letting the LLM grep `git log` and `gh pr list` itself, we hand it a
structured summary. These tests pin the brief's behaviour against fixture
repos so we catch silent format/content regressions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from .conftest import brief


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_repo(tmp_path: Path, *, log_lines=None, goal_md=None) -> Path:
    iter_dir = tmp_path / ".iteration"
    iter_dir.mkdir()
    config = {
        "schema_version": 3,
        "repo_slug": "demo",
        "goal": "ship v1",
        "mode": "goal-driven",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": [
            {
                "id": "prd-implement", "primitive": "scheduled",
                "trigger": {"cron": "0 */12 * * *", "human": "every 12 hours"},
                "purpose": "drive PRD", "automation_level": "auto",
                "state": "ACTIVE", "self_evolve": True, "stagnation_threshold": 5,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
            },
            {
                "id": "session-doc-drift", "primitive": "scheduled",
                "trigger": {"cron": "0 17 * * 1", "human": "5:00 PM Mondays"},
                "purpose": "doc drift", "automation_level": "auto",
                "state": "STOPPED", "self_evolve": False, "stagnation_threshold": 7,
                "stats": {"runs": 0, "useful": 0, "noisy": 0},
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *", "human": "9:00 AM daily",
            "anti_flap_window": 3, "default_stagnation_threshold": 5,
            "budget": "medium",
        },
    }
    (iter_dir / "config.yaml").write_text(yaml.safe_dump(config))
    if log_lines is not None:
        (iter_dir / "log.jsonl").write_text("\n".join(json.dumps(e) for e in log_lines) + "\n")
    if goal_md is not None:
        (iter_dir / "goal.md").write_text(goal_md)
    return tmp_path


# ---------------------------------------------------------------------------
# render() core
# ---------------------------------------------------------------------------

def test_brief_renders_header(tmp_path):
    repo = _write_repo(tmp_path)
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    assert "# coordinator brief" in out
    assert "repo: demo" in out
    assert "mode: goal-driven" in out
    assert "budget: medium" in out


def test_brief_counts_prd_progress(tmp_path):
    goal = """\
# goal
- [x] one
- [x] two
- [ ] three
- [ ] four
- [ ] five
"""
    repo = _write_repo(tmp_path, goal_md=goal)
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    assert "done: 2/5" in out
    assert "three" in out and "four" in out


def test_brief_handles_missing_goal_file(tmp_path):
    repo = _write_repo(tmp_path)  # no goal.md
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    assert "PRD progress" in out
    # No crash — counts are zero.
    assert "done: 0/0" in out


def test_brief_lists_routines_with_state(tmp_path):
    repo = _write_repo(tmp_path)
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    assert "prd-implement" in out
    assert "session-doc-drift" in out
    assert "STOPPED" in out  # state visible
    assert "ACTIVE" in out


def test_brief_aggregates_log_runs(tmp_path):
    log = [
        {"ts": "2026-05-09T10:00:00+0200", "routine": "prd-implement",
         "outcome": "ok", "summary": "PR opened", "increment_signal": True},
        {"ts": "2026-05-09T22:00:00+0200", "routine": "prd-implement",
         "outcome": "warn", "summary": "lint", "increment_signal": False},
        {"ts": "2026-05-09T18:00:00+0200", "routine": "session-doc-drift",
         "outcome": "ok", "summary": "no drift", "increment_signal": False},
    ]
    repo = _write_repo(tmp_path, log_lines=log)
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    # Find the prd-implement row and confirm the counts.
    row = next(line for line in out.splitlines() if line.startswith("| prd-implement"))
    nums = [int(x) for x in row.replace("|", " ").split() if x.isdigit()]
    assert nums == [2, 1, 1]   # runs=2 useful=1 noisy=1


def test_brief_includes_pending_evolve_count(tmp_path):
    repo = _write_repo(tmp_path)
    (repo / ".iteration" / "evolve_requests.jsonl").write_text(
        '{"ts":"x","routine_id":"a","reason":"r","suggested":"s"}\n'
        '{"ts":"y","routine_id":"b","reason":"r","suggested":"s"}\n'
    )
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    assert "Pending evolve requests: 2" in out


def test_brief_shows_recent_coordinator_decisions(tmp_path):
    log = [
        {"ts": "2026-05-09T10:00:00+0200", "routine": "coordinator",
         "outcome": "ok", "summary": "dispatched prd-implement"},
        {"ts": "2026-05-09T22:00:00+0200", "routine": "coordinator",
         "outcome": "noop", "summary": "no work needed"},
    ]
    repo = _write_repo(tmp_path, log_lines=log)
    cfg = yaml.safe_load((repo / ".iteration" / "config.yaml").read_text())
    out = brief.render(cfg, repo)
    assert "dispatched prd-implement" in out
    assert "no work needed" in out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_count_prd_progress_handles_capital_X(tmp_path):
    p = tmp_path / "g.md"
    p.write_text("- [X] done\n- [ ] todo\n")
    done, todo, nxt = brief.count_prd_progress(p)
    assert done == 1 and todo == 1


def test_last_fire_of_returns_most_recent(tmp_path):
    log = [
        {"ts": "1", "routine": "x", "outcome": "ok"},
        {"ts": "2", "routine": "y", "outcome": "ok"},
        {"ts": "3", "routine": "x", "outcome": "warn"},
    ]
    last = brief.last_fire_of(log, "x")
    assert last is not None and last["ts"] == "3" and last["outcome"] == "warn"


def test_last_fire_of_returns_none_for_missing_routine():
    assert brief.last_fire_of([{"routine": "a"}], "b") is None


def test_pending_evolve_count_zero_if_missing(tmp_path):
    assert brief.pending_evolve_count(tmp_path) == 0


# ---------------------------------------------------------------------------
# Locality contract — brief.py must not invoke an LLM
# ---------------------------------------------------------------------------

def test_brief_does_not_invoke_an_llm():
    """The point of the brief is to be cheap. Using subprocess to call out to
    `git`/`gh` is fine; importing or shelling to Claude/Anthropic APIs is not."""
    import re
    src = Path(brief.__file__).read_text()
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = re.sub(r"^\s*#.*$", "", code, flags=re.MULTILINE)
    assert "anthropic" not in code.lower()
    # No literal claude binary invocation. (`subprocess.run(["claude"...])` etc.)
    assert "\"claude\"" not in code
    assert "'claude'" not in code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_main_default_prints_markdown(tmp_path, capsys):
    repo = _write_repo(tmp_path)
    rc = brief.main(["--root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# coordinator brief" in out


def test_main_json_emits_parseable_payload(tmp_path, capsys):
    repo = _write_repo(tmp_path)
    rc = brief.main(["--root", str(repo), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "goal-driven"
    assert payload["budget"] == "medium"
    assert {r["id"] for r in payload["routines"]} == {"prd-implement", "session-doc-drift"}
    assert "prd" in payload and "next_three" in payload["prd"]


def test_main_returns_1_when_no_config(tmp_path):
    with pytest.raises(SystemExit) as exc:
        brief.main(["--root", str(tmp_path)])
    assert exc.value.code == 1
