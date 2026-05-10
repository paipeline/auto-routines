"""
Tests for scripts/dashboard.py render_dashboard() (PRD #10 Module 2, phase 1).

Contract:
    render_dashboard(state, config, log_entries, *, now) -> str  (Markdown)

Pure function: no I/O, no datetime.now(). Returns the body of the living
GitHub issue that the user reads to perceive 'what auto-routines is doing'.

The renderer is the deep module; the sync wrapper that calls
`gh issue edit` is a thin shell around it (phase 2). Keeping render pure
means the dashboard can be unit-tested without ever touching git/gh.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_dashboard():
    spec = importlib.util.spec_from_file_location(
        "dashboard", ROOT / "scripts" / "dashboard.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashboard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def dash():
    return _load_dashboard()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _config(**overrides) -> dict:
    base = {
        "schema_version": 4,
        "repo_slug": "demo-repo",
        "goal": "ship v1",
        "mode": "fully-auto",
        "deps": {"gh": "required", "mcps": ["scheduled-tasks"]},
        "routines": [
            {
                "id": "pr-watcher",
                "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "*/30 * * * *", "human": "every 30 minutes"},
                "purpose": "watch PRs",
                "automation_level": "auto",
                "execution_surface": "gha",
                "est_minutes": 4,
            },
            {
                "id": "daily-digest",
                "state": "ACTIVE",
                "primitive": "scheduled",
                "trigger": {"cron": "0 18 * * *", "human": "6:00 PM daily"},
                "purpose": "summarize today",
                "automation_level": "auto",
                "execution_surface": "local",
                "est_minutes": 5,
            },
        ],
        "neutralized_tasks": [],
        "meta": {
            "cron": "0 9 * * *",
            "human": "9:00 AM daily",
            "anti_flap_window": 7,
            "default_stagnation_threshold": 7,
            "process_evolve_requests": True,
            "idle_window": "22:00-08:00",
            "idle_window_tz": "America/Los_Angeles",
            "gha_minutes_cap": 60,
            "kill_switch": False,
        },
        "last_iter": 7,
    }
    base.update(overrides)
    return base


def _state(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "gha_minutes_used_today": 12,
        "gha_minutes_reset_date": "2026-05-10",
        "last_event_id": 42,
        "kill_switch_active": False,
        "last_dispatch": {
            "pr-watcher": {
                "ts": "2026-05-10T16:30:00-0700",
                "surface": "gha",
                "outcome": "ok",
            },
        },
    }
    base.update(overrides)
    return base


def _now(year=2026, month=5, day=10, hour=17, minute=3) -> dt.datetime:
    return dt.datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo("America/Los_Angeles")
    )


def _log(entries: list[dict] | None = None) -> list[dict]:
    return entries if entries is not None else [
        {
            "ts": "2026-05-10T16:30:00-0700",
            "routine": "pr-watcher",
            "outcome": "ok",
            "summary": "Comment posted on PR #42",
        },
        {
            "ts": "2026-05-10T12:00:00-0700",
            "routine": "pr-watcher",
            "outcome": "noop",
            "summary": "skipped — in idle window",
        },
    ]


# ---------------------------------------------------------------------------
# Smoke / shape
# ---------------------------------------------------------------------------

class TestShape:
    def test_returns_string(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert isinstance(out, str)
        assert len(out) > 100  # not empty / not a stub

    def test_starts_with_h1(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert out.lstrip().startswith("# "), out[:80]

    def test_includes_iter_number(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "iter 7" in out.lower() or "iter-007" in out or "iteration 7" in out.lower()

    def test_pure_no_input_mutation(self, dash):
        import copy
        s, c, l = _state(), _config(), _log()
        snap = (copy.deepcopy(s), copy.deepcopy(c), copy.deepcopy(l))
        dash.render_dashboard(s, c, l, now=_now())
        assert (s, c, l) == snap


# ---------------------------------------------------------------------------
# Status block — kill switch, idle, cost cap
# ---------------------------------------------------------------------------

class TestStatus:
    def test_kill_switch_inactive_shown(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        # Either a dedicated "Kill switch" line or the word "inactive"
        assert "kill switch" in out.lower()
        assert "inactive" in out.lower()

    def test_kill_switch_active_warns_loudly(self, dash):
        out = dash.render_dashboard(
            _state(kill_switch_active=True),
            _config(),
            _log(),
            now=_now(),
        )
        assert "kill switch" in out.lower()
        assert "active" in out.lower()
        # Loud signal — bold/caps/⚠️ — pin one specific marker so the
        # template can't silently drop it.
        assert ("**" in out and "active" in out.lower()) or "⚠" in out or "ACTIVE" in out

    def test_idle_window_displayed(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "22:00-08:00" in out
        assert "America/Los_Angeles" in out

    def test_idle_window_always_says_disabled(self, dash):
        cfg = _config()
        cfg["meta"]["idle_window"] = "always"
        cfg["meta"].pop("idle_window_tz", None)
        out = dash.render_dashboard(_state(), cfg, _log(), now=_now())
        # "always" means no idle window — say so explicitly so users
        # know they aren't getting any blocking.
        assert "no idle window" in out.lower() or "always" in out.lower() or "disabled" in out.lower()

    def test_cost_cap_usage_shown(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "12" in out and "60" in out
        # Should also mention "minutes" so the number isn't ambiguous
        assert "min" in out.lower()

    def test_cost_cap_remaining_shown_explicitly(self, dash):
        """PRD #10 user story 30: 'I want today's GHA-minutes-used and
        REMAINING-cap shown, so that I see budget burn before it
        becomes a surprise bill.' The dashboard already shows used/cap;
        this pin makes sure the *remaining* number is also rendered.
        Without it the user has to do mental subtraction every read —
        defeats the 'glance and know' purpose of the dashboard.

        With used=12, cap=60 → remaining=48 must appear on the cost line.
        """
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "48" in out, (
            "remaining minutes (cap 60 - used 12 = 48) must appear in the "
            "dashboard cost block — PRD #10 story 30 calls out remaining-cap "
            "specifically; computing it client-side defeats the dashboard's "
            "'glance and know' purpose"
        )
        # And it must appear in the same vicinity as the cost numbers,
        # not just in some unrelated word elsewhere. Pin via "remaining"
        # keyword which makes the meaning explicit.
        assert "remaining" in out.lower(), (
            "the cost line must label the remaining number as 'remaining' "
            "(or equivalent); a bare '48' is confusing if a future change "
            "introduces other numbers in the status block"
        )

    def test_cost_cap_over_budget_warns_loudly(self, dash):
        """When today's usage already meets-or-exceeds the cap, the
        dashboard must flag this loudly rather than rendering a
        misleading 125%-but-quiet number. A user paying for GHA minutes
        wants to *see* the surprise bill brewing, not hunt for it.

        We assert the loud signal appears specifically *on the cost
        line* (the line containing both the used and cap numbers),
        not anywhere in the body — `overwritten` in the footer would
        otherwise satisfy a naive 'over' substring search.
        """
        out = dash.render_dashboard(
            _state(gha_minutes_used_today=75),  # cap is 60 → 25% over
            _config(),
            _log(),
            now=_now(),
        )
        # Find the cost line — it has both 75 and 60 on the same line.
        cost_lines = [
            line for line in out.splitlines()
            if "75" in line and "60" in line
        ]
        assert cost_lines, (
            f"could not find the GHA cost line in the dashboard output; "
            f"expected a line containing both '75' (used) and '60' (cap)"
        )
        cost_line = cost_lines[0].lower()
        loud = "⚠" in cost_lines[0] or "over" in cost_line or "exceeded" in cost_line
        assert loud, (
            f"75/60 min today must put a visible warning symbol or phrase "
            f"on the cost line itself (silent overshoot is the bug story 30 "
            f"is trying to prevent); got line: {cost_lines[0]!r}"
        )

    def test_cost_cap_zero_cap_does_not_crash(self, dash):
        """Defensive: a misconfigured cap=0 must not divide-by-zero.
        Sanity-check refuses 0 in config validation, but the dashboard
        is reading from raw state — the orchestrator should never
        produce this, but a corrupt state.json or a partial migration
        could. Render must succeed (degraded) rather than 500."""
        cfg = _config()
        cfg["meta"]["gha_minutes_cap"] = 0
        # Should not raise.
        out = dash.render_dashboard(_state(), cfg, _log(), now=_now())
        # And the cost block should still render *something* — pin that
        # 'GHA' (or 'cost') still appears, so the section isn't dropped
        # silently when cap is degenerate.
        assert "gha" in out.lower() or "cost" in out.lower()


# ---------------------------------------------------------------------------
# Routines table
# ---------------------------------------------------------------------------

class TestRoutinesTable:
    def test_each_routine_shown(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "pr-watcher" in out
        assert "daily-digest" in out

    def test_state_and_surface_shown(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "ACTIVE" in out
        assert "gha" in out
        assert "local" in out

    def test_human_trigger_shown(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "every 30 minutes" in out
        assert "6:00 PM daily" in out

    def test_last_fire_shown_when_present(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        # PR-watcher's last_dispatch ts: 2026-05-10T16:30:00-0700
        # The dashboard may format it variously; check for the date+hour parts.
        assert "16:30" in out

    def test_dash_when_no_last_fire(self, dash):
        """Routines that have never fired should show '—' (not crash, not 'None')."""
        out = dash.render_dashboard(
            _state(last_dispatch={}), _config(), _log(), now=_now()
        )
        assert "None" not in out  # specifically not the literal Python None
        # A dash placeholder somewhere in the routines block
        assert "—" in out or "-" in out


# ---------------------------------------------------------------------------
# Recent activity block
# ---------------------------------------------------------------------------

class TestRecentActivity:
    def test_shows_log_entries_in_reverse_chronological(self, dash):
        log = [
            {"ts": "2026-05-10T10:00:00-0700", "routine": "pr-watcher",
             "outcome": "ok", "summary": "first"},
            {"ts": "2026-05-10T16:30:00-0700", "routine": "pr-watcher",
             "outcome": "ok", "summary": "latest"},
        ]
        out = dash.render_dashboard(_state(), _config(), log, now=_now())
        # 'latest' must appear before 'first' in the rendered output
        assert out.index("latest") < out.index("first")

    def test_caps_log_entries_to_recent(self, dash):
        """Long history shouldn't blow up the issue body. Render at most
        a fixed number — pin to 20 so it stays predictable."""
        log = [
            {"ts": f"2026-05-10T10:{i:02d}:00-0700", "routine": "pr-watcher",
             "outcome": "ok", "summary": f"entry-{i}"}
            for i in range(50)
        ]
        out = dash.render_dashboard(_state(), _config(), log, now=_now())
        # Newest 20 should appear; oldest should not
        assert "entry-49" in out
        assert "entry-0" not in out

    def test_missing_summary_renders_safely(self, dash):
        """Routines occasionally emit log lines without a summary; we
        must not crash."""
        log = [
            {"ts": "2026-05-10T16:30:00-0700", "routine": "pr-watcher",
             "outcome": "ok"},  # no summary
        ]
        out = dash.render_dashboard(_state(), _config(), log, now=_now())
        assert "pr-watcher" in out


# ---------------------------------------------------------------------------
# Footer / how-to-control block
# ---------------------------------------------------------------------------

class TestFooter:
    def test_explains_kill_switch_control(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        # The user must be able to find 'how to pause' in the dashboard.
        assert "kill_switch" in out
        assert "config.yaml" in out

    def test_explains_per_routine_pause(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "STOPPED" in out

    def test_marks_auto_managed(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        # Pin a stable marker so the sync code can detect 'this is OUR
        # issue' and refuse to overwrite a hand-written one.
        assert "auto-routines" in out.lower()
        # The word 'overwritten' or 'auto-managed' — caller relies on it
        assert "auto-managed" in out.lower() or "overwritten" in out.lower()


# ---------------------------------------------------------------------------
# Timestamp / event id (the dashboard is heartbeat-visible)
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_includes_event_id(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        # last_event_id == 42
        assert "42" in out
        assert "event" in out.lower()

    def test_includes_local_iso_timestamp(self, dash):
        """The 'last updated' timestamp must use local ISO 8601 with
        ±HHMM offset (matches state.py / routine-skill.md). Never UTC `Z`."""
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert "2026-05-10T17:03:00-0700" in out
        assert "Z" not in out.replace(" Z ", "").replace("z ", "")  # crude UTC `Z` check

    def test_naive_now_rejected(self, dash):
        """Defensive: naive datetimes silently default to UTC in some
        formatters — we want a loud failure."""
        with pytest.raises(ValueError):
            dash.render_dashboard(
                _state(), _config(), _log(),
                now=dt.datetime(2026, 5, 10, 17, 3),  # no tzinfo
            )


# ---------------------------------------------------------------------------
# Dashboard marker — sync layer relies on it
# ---------------------------------------------------------------------------

class TestMarker:
    def test_marker_constant_exposed(self, dash):
        """The sync layer uses this string to find an existing dashboard
        issue and avoid clobbering hand-written ones. Pin it."""
        assert isinstance(dash.DASHBOARD_MARKER, str)
        assert dash.DASHBOARD_MARKER  # non-empty
        # The marker MUST appear in every rendered dashboard (so old
        # versions stay detectable across template revisions).

    def test_rendered_body_contains_marker(self, dash):
        out = dash.render_dashboard(_state(), _config(), _log(), now=_now())
        assert dash.DASHBOARD_MARKER in out
