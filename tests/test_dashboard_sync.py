"""
Tests for scripts/dashboard.py sync_dashboard() (PRD #10 Module 2, phase 2).

Contract:
    sync_dashboard(body, *, repo, iter_n, gh_run) -> {
        action: "created" | "updated" | "unchanged",
        issue_url: str | None,
        issue_number: int | None,
    }

`gh_run` is dependency-injected — defaults to a subprocess wrapper, but
tests pass a mock so we exercise the decision logic without touching
the network. The pure renderer (phase 1) feeds this; together they
make the dashboard end-to-end.

Invariants pinned by tests:
  - Find an existing dashboard issue by greping for DASHBOARD_MARKER.
  - If none: create one (title contains 'iter N').
  - If exists with matching body: noop ("unchanged" — don't churn the
    issue update timestamp / send a notification on every tick).
  - If exists with differing body: edit it.
  - Refuse to clobber an issue without the marker (a hand-written
    'auto-routines' issue is sacred).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
# Helpers — fake gh_run that records calls and returns canned outputs
# ---------------------------------------------------------------------------

class FakeGh:
    """Recorded `gh_run` mock. Each `gh_run(args)` call is logged.
    Use .add_response() to queue responses keyed by the leading args
    (e.g. ['issue', 'list'], ['issue', 'view']).

    Snapshots any `--body-file FILE` contents at call time, since
    sync_dashboard deletes the tempfile right after the gh call returns.
    Look up via `.body_files[file_path]` in tests."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.body_files: dict[str, str] = {}
        self._responses: list[tuple[tuple[str, ...], str]] = []

    def add_response(self, prefix: list[str], stdout: str) -> None:
        self._responses.append((tuple(prefix), stdout))

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        # Snapshot --body-file content NOW — sync_dashboard deletes it
        # before returning so the test can't read it later.
        for i, a in enumerate(args):
            if a == "--body-file" and i + 1 < len(args):
                fp = args[i + 1]
                try:
                    self.body_files[fp] = Path(fp).read_text()
                except OSError:
                    pass
        for prefix, out in self._responses:
            if tuple(args[: len(prefix)]) == prefix:
                return out
        # Default: no match for find = empty list
        return ""


def _body_with_marker(dash, text: str = "hello") -> str:
    return f"# auto-routines dashboard — iter 7\n\n{text}\n\n{dash.DASHBOARD_MARKER}\n"


# ---------------------------------------------------------------------------
# No existing dashboard → create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_creates_when_no_existing_issue(self, dash):
        gh = FakeGh()
        # `gh issue list` returns no issues
        gh.add_response(["issue", "list"], "[]")
        # `gh issue create` returns the new issue URL on stdout
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/42\n",
        )
        body = _body_with_marker(dash)
        out = dash.sync_dashboard(
            body, repo="owner/repo", iter_n=7, gh_run=gh
        )
        assert out["action"] == "created"
        assert out["issue_url"] == "https://github.com/owner/repo/issues/42"
        assert out["issue_number"] == 42

    def test_create_uses_iter_in_title(self, dash):
        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/9\n",
        )
        dash.sync_dashboard(
            _body_with_marker(dash), repo="owner/repo", iter_n=7, gh_run=gh
        )
        create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        # The title must mention 'iter 7' (or iteration 7) so the user
        # sees the right issue when they search.
        title_arg = _flag_value(create_call, "--title")
        assert "7" in title_arg
        assert "iter" in title_arg.lower() or "iteration" in title_arg.lower()

    def test_create_passes_repo(self, dash):
        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/9\n",
        )
        dash.sync_dashboard(
            _body_with_marker(dash), repo="owner/repo", iter_n=1, gh_run=gh
        )
        create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        assert _flag_value(create_call, "--repo") == "owner/repo"

    def test_create_uses_provided_body(self, dash):
        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/9\n",
        )
        body = _body_with_marker(dash, "specific marker text 123")
        dash.sync_dashboard(body, repo="owner/repo", iter_n=1, gh_run=gh)
        create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        # Body should be passed verbatim — either inline via --body or
        # via --body-file (sync_dashboard uses --body-file to dodge argv
        # length limits). FakeGh snapshots the file content at call time.
        inline = _flag_value(create_call, "--body")
        file_path = _flag_value(create_call, "--body-file")
        body_arg = inline or gh.body_files.get(file_path or "", "")
        assert "specific marker text 123" in body_arg


# ---------------------------------------------------------------------------
# Existing dashboard → update or noop
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_updates_when_body_differs(self, dash):
        gh = FakeGh()
        # gh issue list returns one matching issue
        existing = [{
            "number": 42,
            "title": "auto-routines dashboard — iter 7",
            "url": "https://github.com/owner/repo/issues/42",
            "body": _body_with_marker(dash, "OLD body"),
            "state": "OPEN",
        }]
        gh.add_response(["issue", "list"], json.dumps(existing))
        gh.add_response(["issue", "edit"], "")
        new_body = _body_with_marker(dash, "NEW body")
        out = dash.sync_dashboard(
            new_body, repo="owner/repo", iter_n=7, gh_run=gh
        )
        assert out["action"] == "updated"
        assert out["issue_number"] == 42
        # An edit call should have been made
        edit_calls = [c for c in gh.calls if c[:2] == ["issue", "edit"]]
        assert len(edit_calls) == 1

    def test_unchanged_when_body_matches(self, dash):
        gh = FakeGh()
        body = _body_with_marker(dash, "stable body")
        existing = [{
            "number": 42,
            "title": "auto-routines dashboard — iter 7",
            "url": "https://github.com/owner/repo/issues/42",
            "body": body,
            "state": "OPEN",
        }]
        gh.add_response(["issue", "list"], json.dumps(existing))
        out = dash.sync_dashboard(body, repo="owner/repo", iter_n=7, gh_run=gh)
        assert out["action"] == "unchanged"
        assert out["issue_number"] == 42
        # No edit call (saves a notification ping per tick)
        assert not any(c[:2] == ["issue", "edit"] for c in gh.calls)

    def test_finds_existing_by_marker_not_by_title(self, dash):
        """The renderer might tweak the title format; the marker is the
        stable identity. If only the body has the marker, find it."""
        gh = FakeGh()
        existing = [
            {
                "number": 9,
                "title": "Some old auto-routines thing",
                "url": "https://github.com/owner/repo/issues/9",
                "body": "no marker here",
                "state": "OPEN",
            },
            {
                "number": 11,
                "title": "Random custom title",
                "url": "https://github.com/owner/repo/issues/11",
                "body": _body_with_marker(dash, "real dashboard"),
                "state": "OPEN",
            },
        ]
        gh.add_response(["issue", "list"], json.dumps(existing))
        gh.add_response(["issue", "edit"], "")
        out = dash.sync_dashboard(
            _body_with_marker(dash, "fresh"),
            repo="owner/repo", iter_n=7, gh_run=gh
        )
        assert out["action"] == "updated"
        assert out["issue_number"] == 11


# ---------------------------------------------------------------------------
# Closed dashboard → user signaled "iteration complete" (PRD #10 user story 19)
# ---------------------------------------------------------------------------

class TestClosedRollsIteration:
    """User story 19: 'closing the dashboard issue manually' is the natural
    'ship and move on' gesture. The next tick must not resurrect the closed
    issue with new content — it must open a fresh dashboard. The closed
    issue stays closed as the iteration's archive marker."""

    def test_creates_new_when_existing_dashboard_is_closed(self, dash):
        """The marker is found on a CLOSED issue → treat as 'iteration shipped',
        create a new one rather than updating the closed one."""
        gh = FakeGh()
        existing = [{
            "number": 42,
            "title": "auto-routines dashboard — iter 7",
            "url": "https://github.com/owner/repo/issues/42",
            "body": _body_with_marker(dash, "old iter content"),
            "state": "CLOSED",
        }]
        gh.add_response(["issue", "list"], json.dumps(existing))
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/55\n",
        )
        new_body = _body_with_marker(dash, "iter 8 content")
        out = dash.sync_dashboard(
            new_body, repo="owner/repo", iter_n=8, gh_run=gh,
        )
        # Must create a brand-new issue for iter 8, not edit issue #42.
        assert out["action"] == "created", (
            f"closed dashboard must trigger fresh create, got {out['action']!r}"
        )
        assert out["issue_number"] == 55
        # The closed issue must not be touched.
        assert not any(c[:2] == ["issue", "edit"] for c in gh.calls), (
            "must NOT edit a closed dashboard issue — closing is the user's "
            "ship-and-move-on signal (PRD #10 user story 19)"
        )

    def test_open_takes_precedence_over_closed(self, dash):
        """If both an OPEN and CLOSED dashboard issue exist, the OPEN one is
        the live target. The closed one is iteration history."""
        gh = FakeGh()
        existing = [
            {
                "number": 42,
                "title": "auto-routines dashboard — iter 7",
                "url": "https://github.com/owner/repo/issues/42",
                "body": _body_with_marker(dash, "old closed iter"),
                "state": "CLOSED",
            },
            {
                "number": 50,
                "title": "auto-routines dashboard — iter 8",
                "url": "https://github.com/owner/repo/issues/50",
                "body": _body_with_marker(dash, "live iter"),
                "state": "OPEN",
            },
        ]
        gh.add_response(["issue", "list"], json.dumps(existing))
        gh.add_response(["issue", "edit"], "")
        out = dash.sync_dashboard(
            _body_with_marker(dash, "fresh"),
            repo="owner/repo", iter_n=8, gh_run=gh,
        )
        # Must update the OPEN one (#50), not the closed one.
        assert out["action"] == "updated"
        assert out["issue_number"] == 50

    def test_issue_list_fetches_state_field(self, dash):
        """sync_dashboard's `gh issue list` must request the `state` field
        — without it the open-vs-closed decision can't be made."""
        gh = FakeGh()
        gh.add_response(["issue", "list"], "[]")
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/1\n",
        )
        dash.sync_dashboard(
            _body_with_marker(dash), repo="owner/repo", iter_n=1, gh_run=gh,
        )
        list_call = next(c for c in gh.calls if c[:2] == ["issue", "list"])
        json_fields = _flag_value(list_call, "--json") or ""
        assert "state" in json_fields, (
            "gh issue list must include `state` in --json fields so "
            "sync_dashboard can distinguish open from closed dashboards"
        )


# ---------------------------------------------------------------------------
# Refusal cases
# ---------------------------------------------------------------------------

class TestRefuse:
    def test_refuses_to_sync_body_without_marker(self, dash):
        """Dashboard sync only writes bodies the renderer produced. A
        body without the marker would be unfindable on the next tick."""
        gh = FakeGh()
        with pytest.raises(ValueError, match="marker"):
            dash.sync_dashboard(
                "# something else\n\nno marker here\n",
                repo="owner/repo", iter_n=7, gh_run=gh,
            )

    def test_refuses_when_repo_is_empty(self, dash):
        gh = FakeGh()
        with pytest.raises(ValueError, match="repo"):
            dash.sync_dashboard(
                _body_with_marker(dash), repo="", iter_n=7, gh_run=gh,
            )

    def test_does_not_clobber_unmarked_issue_with_dashboard_in_title(self, dash):
        """A user might have a hand-written 'auto-routines dashboard'
        issue from before this skill. We MUST NOT overwrite it."""
        gh = FakeGh()
        existing = [{
            "number": 99,
            "title": "auto-routines dashboard — iter 7",  # title looks ours
            "url": "https://github.com/owner/repo/issues/99",
            "body": "Hand-written notes from the user. No marker.",
            "state": "OPEN",
        }]
        gh.add_response(["issue", "list"], json.dumps(existing))
        gh.add_response(
            ["issue", "create"],
            "https://github.com/owner/repo/issues/100\n",
        )
        out = dash.sync_dashboard(
            _body_with_marker(dash), repo="owner/repo", iter_n=7, gh_run=gh,
        )
        # Should have CREATED a new issue, not edited the unmarked one
        assert out["action"] == "created"
        assert out["issue_number"] == 100
        assert not any(c[:2] == ["issue", "edit"] for c in gh.calls)


# ---------------------------------------------------------------------------
# gh_run defaults
# ---------------------------------------------------------------------------

class TestDefaultRunner:
    def test_default_gh_run_callable_exists(self, dash):
        """A default subprocess-backed runner must exist so callers can
        omit gh_run in production."""
        assert callable(dash.default_gh_run)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _flag_value(args: list[str], flag: str) -> str | None:
    """Find the value passed for `--flag VALUE` in an argv-style list."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def _flag_file(args: list[str], flag: str) -> str | None:
    """Read the file passed via `--flag FILE` if used (gh issue create
    supports `--body-file`). Returns the file contents."""
    path = _flag_value(args, flag)
    if not path:
        return None
    try:
        return Path(path).read_text()
    except OSError:
        return None
