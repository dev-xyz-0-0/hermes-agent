"""Phase 0 — baseline compatibility snapshot tests.

Pins the current `fact_store`/`fact_feedback` tool contracts, the current
database schema's tables/columns/indexes/triggers, and provider
configuration defaults, so an accidental change to any of them during the
Phase 1+ work in docs/plans/demo-reference-plan/ fails loudly here instead
of silently shipping. See implementation-plan.md section "Phase 0" item 4
and checklist.md section 1 ("Baseline compatibility tests").

These tests intentionally duplicate literal values from the production
source rather than importing and re-deriving them, because the whole point
is to catch an *accidental* change to those literals.
"""

from __future__ import annotations

import sqlite3

from plugins.memory.holographic import FACT_FEEDBACK_SCHEMA, FACT_STORE_SCHEMA, HolographicMemoryProvider
from plugins.memory.holographic.store import MemoryStore, _SCHEMA


class TestToolSchemaSnapshot:
    def test_fact_store_action_enum_is_pinned(self):
        actions = set(FACT_STORE_SCHEMA["parameters"]["properties"]["action"]["enum"])
        assert actions == {
            "add", "search", "probe", "related", "reason", "contradict",
            "update", "remove", "list", "stats", "history", "diagnostics",
            "backup", "share",
        }

    def test_fact_store_requires_action_only(self):
        assert FACT_STORE_SCHEMA["parameters"]["required"] == ["action"]
        assert FACT_STORE_SCHEMA["name"] == "fact_store"

    def test_fact_feedback_action_enum_is_pinned(self):
        actions = set(FACT_FEEDBACK_SCHEMA["parameters"]["properties"]["action"]["enum"])
        assert actions == {"helpful", "unhelpful"}

    def test_fact_feedback_requires_action_and_fact_id(self):
        assert set(FACT_FEEDBACK_SCHEMA["parameters"]["required"]) == {"action", "fact_id"}
        assert FACT_FEEDBACK_SCHEMA["name"] == "fact_feedback"

    def test_get_tool_schemas_returns_exactly_these_two(self):
        provider = HolographicMemoryProvider(config={"db_path": ":memory:"})
        schemas = provider.get_tool_schemas()
        assert [s["name"] for s in schemas] == ["fact_store", "fact_feedback"]


class TestProviderConfigDefaults:
    def test_default_config_values(self):
        provider = HolographicMemoryProvider(config={})
        assert provider._min_trust == 0.3
        assert provider._min_coverage == 0.3
        assert provider._repeat_turns == 6
        assert provider._auto_recall is True
        assert provider._auto_capture is False
        assert provider._consolidation_enabled is True
        assert provider._extraction_mode == "rules"
        assert provider._profile_isolation is True
        assert provider._diagnostics_enabled is True
        assert provider._diagnostics_include_content is False
        assert provider._shared_memory_enabled is False
        assert provider._backup_enabled is False
        assert provider._backup_retention == 14

    def test_config_schema_keys_are_pinned(self):
        provider = HolographicMemoryProvider(config={})
        keys = {field["key"] for field in provider.get_config_schema()}
        expected = {
            "db_path", "auto_extract", "default_trust", "hrr_dim",
            "mirror_memory_writes", "prefetch_min_coverage", "prefetch_repeat_turns",
            "auto_recall", "recall_top_k", "recall_max_tokens", "recall_timeout_ms",
            "recall_debug", "auto_capture", "extraction_mode", "hybrid_capture_timeout_ms",
            "max_facts_per_turn", "consolidation_enabled", "capture_timeout_ms",
            "capture_debug", "profile_isolation", "diagnostics_enabled",
            "diagnostics_include_content", "shared_memory_enabled", "shared_memory_categories",
            "backup_enabled", "backup_retention",
        }
        assert keys == expected


class TestSchemaSnapshot:
    """Pins the current facts/entity/source/bank/tombstone schema."""

    def test_facts_table_columns(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(facts)").fetchall()}
        finally:
            store.close()
        assert columns == {
            "fact_id", "content", "category", "tags", "trust_score",
            "retrieval_count", "helpful_count", "created_at", "updated_at",
            "hrr_vector", "status", "superseded_by", "source_type",
            "extraction_method", "confidence", "namespace",
        }

    def test_expected_tables_exist(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            tables = {
                row[0] for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
        finally:
            store.close()
        for expected in (
            "facts", "entities", "fact_entities", "fact_sources",
            "forgotten_facts", "memory_banks", "facts_fts",
        ):
            assert expected in tables

    def test_expected_indexes_exist(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            indexes = {
                row[0] for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        finally:
            store.close()
        for expected in (
            "idx_facts_trust", "idx_facts_category", "idx_entities_name",
            "idx_fact_sources_fact_id", "idx_facts_namespace", "idx_facts_content_namespace",
        ):
            assert expected in indexes

    def test_expected_fts_triggers_exist(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            triggers = {
                row[0] for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
        finally:
            store.close()
        assert {"facts_ai", "facts_ad", "facts_au"} <= triggers

    def test_forgotten_facts_tombstone_table_shape(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(forgotten_facts)").fetchall()}
        finally:
            store.close()
        assert columns == {"content_hash", "namespace", "forgotten_at"}

    def test_memory_banks_table_shape(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(memory_banks)").fetchall()}
        finally:
            store.close()
        assert columns == {"bank_id", "bank_name", "vector", "dim", "fact_count", "updated_at"}


class TestBaselineRegression:
    """Phase 0 compatibility gate: existing behavior keeps working."""

    def test_existing_databases_open_without_data_loss(self, pre_pr2_db):
        store = MemoryStore(db_path=pre_pr2_db, hrr_dim=32)
        try:
            rows = store._conn.execute("SELECT content FROM facts").fetchall()
            assert [r[0] for r in rows] == ["Legacy fact from before PR 2"]
        finally:
            store.close()

    def test_pre_pr3_database_migrates_and_keeps_data(self, pre_pr3_db):
        store = MemoryStore(db_path=pre_pr3_db, hrr_dim=32)
        try:
            rows = store._conn.execute("SELECT content, namespace FROM facts").fetchall()
            assert [tuple(r) for r in rows] == [("Legacy fact from before PR 3 (global unique content)", "")]
            # The migration must have dropped the global UNIQUE(content) index.
            unique_on_content_alone = False
            for idx in store._conn.execute("PRAGMA index_list(facts)").fetchall():
                if not idx["unique"]:
                    continue
                cols = [r["name"] for r in store._conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
                if cols == ["content"]:
                    unique_on_content_alone = True
            assert unique_on_content_alone is False
        finally:
            store.close()

    def test_cli_remains_unscoped(self, unscoped_cli_provider):
        assert unscoped_cli_provider._namespace == ""
        assert unscoped_cli_provider._isolation_blocked is False

    def test_two_gateway_namespaces_stay_isolated(self, two_gateway_namespaces):
        chat_a, chat_b = two_gateway_namespaces
        chat_a._store.add_fact("A's own fact", category="general", namespace=chat_a._namespace)
        assert chat_a._retriever.search("A's own fact", min_trust=0.0, namespace=chat_a._namespace)
        assert chat_b._retriever.search("A's own fact", min_trust=0.0, namespace=chat_b._namespace) == []

    def test_threaded_chat_gets_its_own_namespace(self, threaded_chat_namespace):
        assert threaded_chat_namespace._namespace
        assert "thread_42" in threaded_chat_namespace._namespace

    def test_numpy_unavailable_mode_retains_fts_search(self, tmp_path, numpy_disabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("FTS keyword search still works without numpy", category="general")
            results = store.search_facts("keyword search", min_trust=0.0)
            assert results
        finally:
            store.close()

    def test_current_schema_mixed_status_fixture_shapes(self, current_schema_mixed_status_db):
        db_path, ids = current_schema_mixed_status_db
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            active = store.list_facts(limit=50)
            history = store.get_history(limit=50)
            assert any(f["fact_id"] == ids["active_id"] for f in active)
            assert all(f["fact_id"] != ids["superseded_id"] for f in active)
            assert any(f["fact_id"] == ids["superseded_id"] for f in history)
            tombstones = store.get_tombstones()
            assert len(tombstones) == 1
        finally:
            store.close()
