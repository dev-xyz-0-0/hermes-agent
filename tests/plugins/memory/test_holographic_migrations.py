"""Phase 10 ("Database migrations") tests for the Holographic plugin.

See docs/plans/demo-reference-plan/implementation-plan.md section
"7. Database Migration Plan" and checklist.md section "10. Database
migrations" / "Migration tests". These tests exercise:

  - the explicit `schema_metadata.schema_version` tracking added this
    phase (MemoryStore._detect_schema_generation /
    MemoryStore._finalize_schema_version / MemoryStore.get_schema_version);
  - the pre-PR2 and pre-PR3 migration paths (reusing the Phase 0 fixtures
    in tests/plugins/memory/conftest.py — NOT re-creating them);
  - the pre-commit validation gate (PRAGMA foreign_key_check + row-count
    parity) added to the global-UNIQUE(content) table rebuild, including
    an injected-failure test proving the original database survives
    untouched; and
  - idempotency of the whole migration path on an already-current
    database.

All tests use `tmp_path`-backed databases only — never `~/.hermes/`
(enforced repo-wide by the autouse `_isolate_hermes_home` fixture in
tests/conftest.py).
"""

from __future__ import annotations

import sqlite3

import pytest

from plugins.memory.holographic import diagnostics
from plugins.memory.holographic.store import (
    CURRENT_SCHEMA_VERSION,
    MemoryStore,
    MigrationValidationError,
)
from tests.plugins.memory.conftest import _PRE_PR2_SCHEMA


# ---------------------------------------------------------------------------
# Helpers for building migration scenarios not already covered by the
# Phase 0 pre_pr2_db/pre_pr3_db fixtures (supersession chains, provenance,
# tombstones on a pre-current-schema database).
# ---------------------------------------------------------------------------

def _seed_pre_pr3_db_with_history(db_path) -> dict:
    """A pre-PR3-shaped database (PR2 columns present, global UNIQUE(content)
    still present, no `namespace` column) containing:

      - one active fact;
      - one superseded fact pointing at the active one via `superseded_by`;
      - a `fact_sources` row (provenance) for the active fact;
      - a `forgotten_facts` row (a tombstone) for unrelated forgotten text.

    `fact_sources` and `forgotten_facts` are built with exactly the column
    shapes `plugins/memory/holographic/store.py`'s `_SCHEMA` defines for
    them (they predate the pre-PR3 global-unique-drop migration in real
    history, just like `facts` does), so this reproduces what the facts
    rebuild must preserve when it also renames/recreates those tables.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PRE_PR2_SCHEMA)
        for ddl in (
            "ALTER TABLE facts ADD COLUMN status TEXT DEFAULT 'active'",
            "ALTER TABLE facts ADD COLUMN superseded_by INTEGER",
            "ALTER TABLE facts ADD COLUMN source_type TEXT DEFAULT ''",
            "ALTER TABLE facts ADD COLUMN extraction_method TEXT DEFAULT ''",
            "ALTER TABLE facts ADD COLUMN confidence REAL DEFAULT 1.0",
        ):
            conn.execute(ddl)
        conn.execute(
            """
            CREATE TABLE fact_sources (
                source_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id    INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
                via        TEXT DEFAULT '',
                platform   TEXT DEFAULT '',
                chat_id    TEXT DEFAULT '',
                thread_id  TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                author     TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE TABLE forgotten_facts ("
            "content_hash TEXT NOT NULL, namespace TEXT NOT NULL DEFAULT '', "
            "forgotten_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (content_hash, namespace))"
        )

        cur = conn.execute(
            "INSERT INTO facts (content, category, tags, trust_score, status, confidence) "
            "VALUES (?, 'project', '', 0.5, 'active', 1.0)",
            ("The corrected, current value.",),
        )
        new_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO facts (content, category, tags, trust_score, status, superseded_by, confidence) "
            "VALUES (?, 'project', '', 0.5, 'superseded', ?, 1.0)",
            ("The old value before a correction.", new_id),
        )
        old_id = cur.lastrowid

        conn.execute(
            "INSERT INTO fact_sources (fact_id, via, platform, chat_id) VALUES (?, 'rule', 'cli', '')",
            (new_id,),
        )

        import hashlib
        forgotten_hash = hashlib.sha256(b"A fact nobody wants anymore.").hexdigest()
        conn.execute(
            "INSERT INTO forgotten_facts (content_hash, namespace) VALUES (?, '')",
            (forgotten_hash,),
        )
        conn.commit()
        return {"new_id": new_id, "old_id": old_id, "forgotten_hash": forgotten_hash}
    finally:
        conn.close()


def _schema_dump(db_path) -> list[tuple]:
    """A stable, orderable snapshot of every object's defining SQL —
    used to assert "no schema change happened" across a reopen."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        return list(rows)
    finally:
        conn.close()


def _facts_dump(db_path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT fact_id, content, category, tags, trust_score, status, superseded_by, "
            "source_type, extraction_method, confidence, namespace FROM facts ORDER BY fact_id"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fresh database
# ---------------------------------------------------------------------------

class TestFreshDatabase:
    def test_fresh_db_creates_latest_schema(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            assert store._last_detected_schema_generation == "fresh"
            assert store.get_schema_version() == CURRENT_SCHEMA_VERSION

            report = diagnostics.run_diagnostics(db_path)
            assert report.ok is True
            assert report.exit_code == diagnostics.EXIT_OK

            tables = {
                row[0] for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert {
                "facts", "entities", "fact_entities", "fact_sources",
                "forgotten_facts", "memory_banks", "entity_relationships",
                "schema_metadata",
            } <= tables

            triggers = {
                row[0] for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            assert {"facts_ai", "facts_ad", "facts_au"} <= triggers
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Pre-PR2 / pre-PR3 migration — reusing the Phase 0 fixtures
# ---------------------------------------------------------------------------

class TestLegacyFixturesMigrate:
    def test_pre_pr2_fixture_migrates_successfully(self, pre_pr2_db):
        store = MemoryStore(db_path=pre_pr2_db, hrr_dim=32)
        try:
            assert store._last_detected_schema_generation == "pre_pr2"
            assert store.get_schema_version() == CURRENT_SCHEMA_VERSION
            rows = store._conn.execute("SELECT content, namespace FROM facts").fetchall()
            assert [tuple(r) for r in rows] == [("Legacy fact from before PR 2", "")]
        finally:
            store.close()

        report = diagnostics.run_diagnostics(pre_pr2_db)
        assert report.ok is True

    def test_pre_pr3_fixture_migrates_successfully(self, pre_pr3_db):
        store = MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        try:
            assert store._last_detected_schema_generation == "pre_pr3"
            assert store.get_schema_version() == CURRENT_SCHEMA_VERSION
            rows = store._conn.execute("SELECT content, namespace FROM facts").fetchall()
            assert [tuple(r) for r in rows] == [
                ("Legacy fact from before PR 3 (global unique content)", "")
            ]
            # The global UNIQUE(content) constraint must be gone.
            assert store._has_unique_on_content_alone() is False
        finally:
            store.close()

        report = diagnostics.run_diagnostics(pre_pr3_db)
        assert report.ok is True

    @pytest.mark.parametrize("fixture_name", ["pre_pr2_db", "pre_pr3_db"])
    def test_facts_and_categories_survive_migration(self, fixture_name, request):
        db_path = request.getfixturevalue(fixture_name)
        before = sqlite3.connect(str(db_path)).execute(
            "SELECT content, category FROM facts ORDER BY fact_id"
        ).fetchall()

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            after = store._conn.execute(
                "SELECT content, category FROM facts ORDER BY fact_id"
            ).fetchall()
        finally:
            store.close()

        assert [tuple(r) for r in after] == before

    def test_fts_search_works_after_migration(self, pre_pr3_db):
        store = MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        try:
            results = store.search_facts("Legacy fact")
            assert len(results) == 1
            assert "Legacy fact from before PR 3" in results[0]["content"]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_current_db_migration_is_idempotent(self, current_schema_mixed_status_db):
        db_path, _ids = current_schema_mixed_status_db

        schema_before = _schema_dump(db_path)
        facts_before = _facts_dump(db_path)

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        assert store._last_detected_schema_generation == "current"
        store.close()

        schema_after = _schema_dump(db_path)
        facts_after = _facts_dump(db_path)

        assert schema_after == schema_before
        assert facts_after == facts_before

    def test_legacy_migration_is_idempotent_on_second_open(self, pre_pr3_db):
        store1 = MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        store1.close()

        schema_after_first = _schema_dump(pre_pr3_db)
        facts_after_first = _facts_dump(pre_pr3_db)

        store2 = MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        assert store2._last_detected_schema_generation == "current"
        store2.close()

        assert _schema_dump(pre_pr3_db) == schema_after_first
        assert _facts_dump(pre_pr3_db) == facts_after_first


# ---------------------------------------------------------------------------
# History, provenance, and tombstones survive a real migration
# ---------------------------------------------------------------------------

class TestHistoryAndProvenanceSurvive:
    def test_active_superseded_and_revision_links_survive(self, tmp_path):
        db_path = tmp_path / "history.db"
        ids = _seed_pre_pr3_db_with_history(db_path)

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            row = store._conn.execute(
                "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (ids["old_id"],)
            ).fetchone()
            assert row["status"] == "superseded"
            assert row["superseded_by"] == ids["new_id"]

            new_row = store._conn.execute(
                "SELECT status FROM facts WHERE fact_id = ?", (ids["new_id"],)
            ).fetchone()
            assert new_row["status"] == "active"

            history = store.get_history()
            assert any(h["fact_id"] == ids["old_id"] and h["superseded_by"] == ids["new_id"] for h in history)

            # Normal listing/search never surfaces the superseded fact.
            active_ids = {f["fact_id"] for f in store.list_facts(limit=100)}
            assert ids["old_id"] not in active_ids
            assert ids["new_id"] in active_ids
        finally:
            store.close()

    def test_provenance_survives_migration(self, tmp_path):
        db_path = tmp_path / "provenance.db"
        ids = _seed_pre_pr3_db_with_history(db_path)

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            sources = store._conn.execute(
                "SELECT fact_id, via, platform FROM fact_sources WHERE fact_id = ?", (ids["new_id"],)
            ).fetchall()
            assert len(sources) == 1
            assert sources[0]["via"] == "rule"
            assert sources[0]["platform"] == "cli"
        finally:
            store.close()

    def test_tombstones_survive_migration(self, tmp_path):
        db_path = tmp_path / "tombstones.db"
        ids = _seed_pre_pr3_db_with_history(db_path)

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            assert ids["forgotten_hash"] in store.get_tombstones()
        finally:
            store.close()

    def test_tombstones_survive_noop_migration_of_current_db(self, current_schema_mixed_status_db):
        db_path, _ids = current_schema_mixed_status_db
        tombstones_before = sqlite3.connect(str(db_path)).execute(
            "SELECT content_hash, namespace FROM forgotten_facts"
        ).fetchall()
        assert tombstones_before  # the fixture always has exactly one

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            tombstones_after = store._conn.execute(
                "SELECT content_hash, namespace FROM forgotten_facts"
            ).fetchall()
        finally:
            store.close()

        assert [tuple(r) for r in tombstones_after] == tombstones_before


# ---------------------------------------------------------------------------
# Relationship-scope migration
# ---------------------------------------------------------------------------

class TestRelationshipScopeMigration:
    @pytest.mark.parametrize("fixture_name", ["pre_pr2_db", "pre_pr3_db"])
    def test_missing_relationships_table_is_created_empty_not_synthesized(self, fixture_name, request):
        db_path = request.getfixturevalue(fixture_name)
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            assert store._table_exists("entity_relationships") is True
            count = store._conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
            assert count == 0
        finally:
            store.close()

    def test_existing_relationships_table_is_a_noop(self, current_schema_mixed_status_db):
        db_path, ids = current_schema_mixed_status_db

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            # Create an entity + a namespace-scoped relationship so there is
            # something a broken migration COULD disturb.
            entity_a = store._resolve_entity("Alice")
            entity_b = store._resolve_entity("Bob")
            rel_id = store.upsert_relationship(entity_a, entity_b, "knows", namespace="")
        finally:
            store.close()

        before = sqlite3.connect(str(db_path)).execute(
            "SELECT relationship_id, source_entity_id, target_entity_id, relationship_type, namespace "
            "FROM entity_relationships ORDER BY relationship_id"
        ).fetchall()
        assert before  # at least the one we just inserted

        store2 = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            assert store2._conn.execute(
                "SELECT relationship_id FROM entity_relationships WHERE relationship_id = ?", (rel_id,)
            ).fetchone() is not None
        finally:
            store2.close()

        after = sqlite3.connect(str(db_path)).execute(
            "SELECT relationship_id, source_entity_id, target_entity_id, relationship_type, namespace "
            "FROM entity_relationships ORDER BY relationship_id"
        ).fetchall()
        assert after == before


# ---------------------------------------------------------------------------
# Injected migration failure — the safety-critical case
# ---------------------------------------------------------------------------

class TestInjectedMigrationFailure:
    def test_failed_validation_preserves_the_original_database(self, pre_pr3_db, monkeypatch):
        original_rows = sqlite3.connect(str(pre_pr3_db)).execute(
            "SELECT content FROM facts"
        ).fetchall()

        def _force_failure(self, *_args, **_kwargs):
            raise MigrationValidationError("forced failure for test")

        monkeypatch.setattr(MemoryStore, "_validate_facts_rebuild", _force_failure)

        with pytest.raises(MigrationValidationError):
            MemoryStore(db_path=pre_pr3_db, hrr_dim=32)

        monkeypatch.undo()

        # Reopen with a FRESH, independent sqlite3 connection (not through
        # MemoryStore) — this is the only way to be sure the on-disk file,
        # not some in-process cached state, is what is being inspected.
        conn = sqlite3.connect(str(pre_pr3_db))
        try:
            unique_on_content_alone = False
            for idx in conn.execute("PRAGMA index_list(facts)").fetchall():
                if not idx[2]:
                    continue
                cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
                if cols == ["content"]:
                    unique_on_content_alone = True
            # The DESTRUCTIVE step (the table rebuild that drops the global
            # UNIQUE(content) constraint) must have been fully rolled back:
            # the old constraint is still there, and no row was lost or
            # altered. Note: the separate, non-destructive `ALTER TABLE
            # facts ADD COLUMN namespace ...` step that runs earlier in
            # `_init_db` (before the rebuild is ever attempted) is NOT
            # part of this transaction and is intentionally allowed to have
            # already committed — adding a nullable, defaulted column is
            # additive and harmless even on its own (see the module
            # docstring on `schema_metadata` for the same reasoning applied
            # to older code tolerating unknown additive tables/columns), so
            # rolling it back would add complexity without reducing risk.
            assert unique_on_content_alone is True

            rows = conn.execute("SELECT content FROM facts").fetchall()
            assert rows == original_rows
        finally:
            conn.close()

    def test_retry_after_injected_failure_succeeds(self, pre_pr3_db, monkeypatch):
        def _force_failure(self, *_args, **_kwargs):
            raise MigrationValidationError("forced failure for test")

        monkeypatch.setattr(MemoryStore, "_validate_facts_rebuild", _force_failure)
        with pytest.raises(MigrationValidationError):
            MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        monkeypatch.undo()

        # A later open (e.g. after whatever transient condition caused the
        # failure has cleared) must succeed and fully migrate — the
        # migration is idempotent/restartable, never permanently wedged.
        store = MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        try:
            assert store._has_unique_on_content_alone() is False
            assert store.get_schema_version() == CURRENT_SCHEMA_VERSION
            rows = store._conn.execute("SELECT content, namespace FROM facts").fetchall()
            assert [tuple(r) for r in rows] == [
                ("Legacy fact from before PR 3 (global unique content)", "")
            ]
        finally:
            store.close()

        report = diagnostics.run_diagnostics(pre_pr3_db)
        assert report.ok is True


# ---------------------------------------------------------------------------
# Reopen after a successful migration remains healthy
# ---------------------------------------------------------------------------

class TestReopenRemainsHealthy:
    @pytest.mark.parametrize("fixture_name", ["pre_pr2_db", "pre_pr3_db"])
    def test_reopen_after_migration_is_healthy(self, fixture_name, request):
        db_path = request.getfixturevalue(fixture_name)
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.close()

        report = diagnostics.run_diagnostics(db_path)
        assert report.ok is True
        assert report.exit_code == diagnostics.EXIT_OK
        assert not [i for i in report.issues if i.severity == "error"]
