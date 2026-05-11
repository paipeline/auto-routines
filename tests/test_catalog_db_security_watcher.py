"""
Drift detectors for the `db-security-watcher` archetype — issue #80
(PRD #74).

Targets the "DB foot-guns" class: schema migrations that break
replication, queries that table-scan large tables, missing indexes
on foreign keys. Fires on commit (post-commit git-hook) when the
diff touches migration files or SQL.

Without this archetype, the catalog has no surface for catching
risky migrations *before* they ship — pr-review-bot is generic and
won't enforce DB-specific patterns.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "templates" / "routine-catalog.yaml"


@pytest.fixture(scope="module")
def catalog() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text())


@pytest.fixture(scope="module")
def archetype(catalog) -> dict:
    arch = next(
        (a for a in catalog["archetypes"] if a["id"] == "db-security-watcher"),
        None,
    )
    assert arch is not None, (
        "db-security-watcher archetype is required (issue #80). "
        "Without it, risky migrations slip through unchallenged — "
        "pr-review-bot is generic and won't enforce DB-specific "
        "patterns."
    )
    return arch


class TestDbSecurityWatcherShape:
    def test_primitive_is_git_hook(self, archetype):
        assert archetype["primitive"] == "git-hook", (
            f"db-security-watcher must fire on commit (post-commit "
            f"git-hook) so risky migrations are caught at the "
            f"earliest possible point; got {archetype['primitive']!r}"
        )

    def test_category_is_reactive(self, archetype):
        assert archetype.get("category") == "reactive", (
            "db-security-watcher reacts to a commit touching migration "
            "files — it doesn't drive new feature work forward"
        )

    def test_has_path_filters_for_migrations_and_sql(self, archetype):
        # The whole point of being a git-hook is path-filter elevation —
        # we only fire when migration / SQL files actually changed.
        # Without filters the routine would fire on every commit and
        # noop most of them.
        filters = archetype.get("path_filters") or []
        assert isinstance(filters, list) and len(filters) >= 1, (
            "db-security-watcher must declare path_filters scoped to "
            "migration / SQL paths — without filters the hook fires "
            "on every commit and burns time on noops"
        )
        # At least one filter must target the migration / SQL surface.
        joined = " ".join(filters).lower()
        assert any(
            token in joined
            for token in ["migration", ".sql", "schema"]
        ), (
            f"db-security-watcher path_filters must include at least "
            f"one migration / *.sql / schema pattern; got {filters!r}"
        )

    def test_automation_default_is_notify(self, archetype):
        # Issue #80 spec: notify (find + log; don't auto-rewrite the
        # migration). A risky migration usually needs human judgment.
        assert archetype["automation_default"] == "notify", (
            f"db-security-watcher must default to `notify` — risky "
            f"migrations need human judgment, not auto-rewrites. "
            f"Got {archetype['automation_default']!r}"
        )

    def test_self_evolve_is_false(self, archetype):
        assert archetype["self_evolve"] is False, (
            "db-security-watcher must NOT self_evolve — the risky-"
            "pattern catalog is the security contract; mid-run self-"
            "edits would silently weaken detection"
        )

    def test_success_criterion_uses_predicate_kind(self, archetype):
        sc = archetype["success_criterion"]
        assert isinstance(sc, dict), (
            f"db-security-watcher success_criterion must be a "
            f"structured predicate dict (issue #76 union); got "
            f"{type(sc).__name__}"
        )
        assert sc.get("kind") == "no-failures-n-days", (
            f"db-security-watcher success_criterion.kind must be "
            f"`no-failures-n-days`; got {sc.get('kind')!r}"
        )
        args = sc.get("args") or {}
        assert "days" in args

    def test_stack_hints_cover_common_orms(self, archetype):
        hints = archetype.get("stack_hints") or []
        assert isinstance(hints, list)
        # The interview proposes archetypes whose stack_hints match
        # the detected stack. db-security-watcher applies to any repo
        # with a migration system — pin a handful of common ones so
        # the candidate list surfaces it for Python/Node/Go shops.
        hint_str = " ".join(hints).lower()
        assert any(
            t in hint_str
            for t in ["alembic", "sqlalchemy", "prisma", "knex", "gorm",
                      "migrations"]
        ), (
            f"db-security-watcher stack_hints must cover common "
            f"migration tools (alembic / sqlalchemy / prisma / knex "
            f"/ gorm / migrations dir); got {hints!r}"
        )


class TestDbSecurityWatcherPromptBody:
    """The risky-pattern catalog must be encoded in the prompt so
    the routine actually looks for the right shapes."""

    def test_body_scans_diff_not_full_files(self, archetype):
        body = archetype["prompt_body"].lower()
        assert "diff" in body, (
            "db-security-watcher must scan the *diff* (the added "
            "lines of the commit), not full files — full-file scans "
            "would flag every existing migration repeatedly"
        )

    def test_body_names_risky_patterns(self, archetype):
        # The risky-pattern catalog from issue #80 — DROP COLUMN
        # without backfill, ALTER TYPE without USING, missing FK
        # indexes, UPDATE/DELETE without WHERE, missing transaction,
        # SELECT *, N+1.
        body = archetype["prompt_body"].lower()
        required_patterns = [
            "drop column",
            "alter type",
            ("foreign key", "fk"),
            ("update", "delete"),
            "where",
            "select *",
        ]
        missing = []
        for needle in required_patterns:
            if isinstance(needle, tuple):
                if not any(n in body for n in needle):
                    missing.append(needle)
            else:
                if needle not in body:
                    missing.append(needle)
        assert not missing, (
            f"db-security-watcher prompt_body must enumerate the "
            f"risky-pattern catalog from issue #80; missing: "
            f"{missing}. Without naming each pattern explicitly the "
            f"LLM will skip the less-obvious ones."
        )

    def test_body_logs_findings(self, archetype):
        body = archetype["prompt_body"]
        # automation_default: notify means findings go to log.jsonl —
        # but the routine must explicitly say so or the LLM might
        # default to "do nothing" once it sees automation_level=notify.
        assert ".iteration/log.jsonl" in body, (
            "db-security-watcher must reference log.jsonl as the "
            "output surface — automation_level=notify means findings "
            "go to the log, and the prompt must say so explicitly"
        )
        assert "increment_signal" in body, (
            "db-security-watcher must reference increment_signal — "
            "the meta-agent's stagnation detection depends on it"
        )

    def test_body_describes_what_each_pattern_means(self, archetype):
        """The catalog of patterns is only useful if the prompt also
        explains *why* each is risky — without that, the LLM can't
        explain the finding to the user in the resulting comment."""
        body = archetype["prompt_body"].lower()
        # Look for risk-explanation idioms — "replication", "table
        # scan", "production", "downtime", "lock", "backfill" — at
        # least three so a single-axis prompt fails.
        risk_idioms = [
            "replicat", "table scan", "production", "downtime",
            "lock", "backfill", "rollback", "index",
        ]
        present = sum(1 for w in risk_idioms if w in body)
        assert present >= 3, (
            f"db-security-watcher prompt_body must reference at "
            f"least 3 risk axes (replication / locks / downtime / "
            f"backfill / etc.) so the routine can explain WHY a "
            f"pattern is risky, not just flag it. Found {present}."
        )


class TestDbSecurityWatcherIsCommentOnly:
    """The routine writes findings (notify) and optionally comments
    on a PR — it does not branch + commit code. Must be in
    `COMMENT_ONLY_ARCHETYPES` so the global branch/commit body check
    exempts it."""

    def test_listed_in_comment_only_set(self):
        from tests import test_catalog as t_catalog
        assert "db-security-watcher" in t_catalog.COMMENT_ONLY_ARCHETYPES, (
            "db-security-watcher emits findings rather than committing "
            "code (automation_default: notify) — it must be listed in "
            "`COMMENT_ONLY_ARCHETYPES` so the global branch/commit "
            "body check exempts it. Otherwise the archetype is forced "
            "to grow a vestigial commit step."
        )
