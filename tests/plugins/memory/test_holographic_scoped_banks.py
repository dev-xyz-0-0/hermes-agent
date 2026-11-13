"""Phase 2 — namespace-specific HRR bank tests.

See docs/plans/demo-reference-plan/checklist.md section 5 ("Phase 2 —
Namespace-specific HRR banks" and its "Scoped-bank tests" subsection) and
implementation-plan.md ("Phase 2 — Namespace-specific HRR banks" and
section 9.3, "HRR bank tests").
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from plugins.memory.holographic import holographic as hrr
from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore, bank_name


def _bank_row(store: MemoryStore, name: str):
    return store._conn.execute(
        "SELECT bank_name, vector, dim, fact_count FROM memory_banks WHERE bank_name = ?",
        (name,),
    ).fetchone()


class TestBankKeyIdentity:
    def test_same_category_two_namespaces_different_keys(self):
        key_a = bank_name("chat_A", "general")
        key_b = bank_name("chat_B", "general")
        assert key_a != key_b

    def test_bank_keys_contain_no_raw_identity(self):
        namespace = "telegram:super-secret-chat-id-12345"
        key = bank_name(namespace, "general")
        assert namespace not in key
        assert "super-secret-chat-id" not in key
        assert "telegram" not in key

    def test_unscoped_namespace_preserves_legacy_key(self):
        """namespace == "" keeps the original cat:<category> identity so
        pre-Phase-2 unscoped CLI banks remain usable without migration."""
        assert bank_name("", "general") == "cat:general"

    def test_scoped_key_deterministic(self):
        assert bank_name("chat_A", "general") == bank_name("chat_A", "general")

    def test_scoped_key_differs_by_category_too(self):
        assert bank_name("chat_A", "general") != bank_name("chat_A", "project")


@pytest.fixture
def numpy_enabled():
    if not hrr._HAS_NUMPY:
        pytest.skip("numpy is not installed in this environment")
    return True


class TestBankContents:
    def test_bank_vectors_include_only_their_namespace(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("Alice lives in Paris.", category="general", namespace="chat_A")
            store.add_fact("Bob lives in Berlin.", category="general", namespace="chat_B")

            row_a = _bank_row(store, bank_name("chat_A", "general"))
            row_b = _bank_row(store, bank_name("chat_B", "general"))
            assert row_a is not None
            assert row_b is not None
            assert row_a["fact_count"] == 1
            assert row_b["fact_count"] == 1
            # The two namespace banks must not be bit-identical (they were
            # built from different facts).
            assert row_a["vector"] != row_b["vector"]

            # No legacy cross-namespace aggregate bank should exist.
            legacy = _bank_row(store, "cat:general")
            assert legacy is None
        finally:
            store.close()

    def test_superseded_facts_absent_from_rebuilt_bank(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            old_id = store.add_fact("The sky is blue.", category="general", namespace="chat_A")
            row_before = _bank_row(store, bank_name("chat_A", "general"))
            assert row_before["fact_count"] == 1

            store.supersede_fact(old_id, "The sky is actually grey today.", namespace="chat_A")
            row_after = _bank_row(store, bank_name("chat_A", "general"))
            # Still exactly one *active* fact contributing (the new one) —
            # the superseded original must not also be counted.
            assert row_after["fact_count"] == 1

        finally:
            store.close()

    def test_forget_removes_fact_contribution(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            id1 = store.add_fact("Fact one.", category="general", namespace="chat_A")
            store.add_fact("Fact two.", category="general", namespace="chat_A")
            row = _bank_row(store, bank_name("chat_A", "general"))
            assert row["fact_count"] == 2

            store.remove_fact(id1, namespace="chat_A", tombstone=True)
            row = _bank_row(store, bank_name("chat_A", "general"))
            assert row["fact_count"] == 1
        finally:
            store.close()

    def test_forget_last_fact_deletes_bank_row(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            id1 = store.add_fact("Only fact.", category="general", namespace="chat_A")
            assert _bank_row(store, bank_name("chat_A", "general")) is not None

            store.remove_fact(id1, namespace="chat_A", tombstone=True)
            assert _bank_row(store, bank_name("chat_A", "general")) is None
        finally:
            store.close()


class TestRebuildScoping:
    def test_mutation_rebuilds_only_affected_pair(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("Seed fact for chat A.", category="general", namespace="chat_A")
            store.add_fact("Seed fact for chat B.", category="project", namespace="chat_B")

            calls: list[tuple[str, str]] = []
            original = store.rebuild_bank

            def spy(category, namespace=""):
                calls.append((namespace, category))
                return original(category, namespace)

            with patch.object(store, "rebuild_bank", side_effect=spy):
                store.add_fact("Another fact for chat A.", category="general", namespace="chat_A")

            assert calls == [("chat_A", "general")]
        finally:
            store.close()

    def test_update_fact_rebuilds_only_affected_pair(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            fid = store.add_fact("Needs an update.", category="general", namespace="chat_A")
            store.add_fact("Unrelated chat B fact.", category="general", namespace="chat_B")

            calls: list[tuple[str, str]] = []
            original = store.rebuild_bank

            def spy(category, namespace=""):
                calls.append((namespace, category))
                return original(category, namespace)

            with patch.object(store, "rebuild_bank", side_effect=spy):
                store.update_fact(fid, content="Updated content.", namespace="chat_A")

            assert calls == [("chat_A", "general")]
        finally:
            store.close()

    def test_update_fact_category_change_rebuilds_both_pairs(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            fid = store.add_fact("Recategorize me.", category="general", namespace="chat_A")

            calls: list[tuple[str, str]] = []
            original = store.rebuild_bank

            def spy(category, namespace=""):
                calls.append((namespace, category))
                return original(category, namespace)

            with patch.object(store, "rebuild_bank", side_effect=spy):
                store.update_fact(fid, category="project", namespace="chat_A")

            assert set(calls) == {("chat_A", "project"), ("chat_A", "general")}
        finally:
            store.close()

    def test_batch_rebuild_count_bounded_to_affected_pairs(self, tmp_path, numpy_enabled):
        """rebuild_all_vectors must rebuild each affected (namespace,
        category) pair once — not once per fact."""
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            # 5 facts across only 2 distinct (namespace, category) pairs.
            for i in range(3):
                store.add_fact(f"Chat A general fact {i}.", category="general", namespace="chat_A")
            for i in range(2):
                store.add_fact(f"Chat B project fact {i}.", category="project", namespace="chat_B")

            calls: list[tuple[str, str]] = []
            original = store.rebuild_bank

            def spy(category, namespace=""):
                calls.append((namespace, category))
                return original(category, namespace)

            with patch.object(store, "rebuild_bank", side_effect=spy):
                processed = store.rebuild_all_vectors()

            assert processed == 5
            assert len(calls) == 2
            assert set(calls) == {("chat_A", "general"), ("chat_B", "project")}
        finally:
            store.close()

    def test_rebuild_all_vectors_scoped_to_one_namespace(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("Chat A fact.", category="general", namespace="chat_A")
            store.add_fact("Chat B fact.", category="general", namespace="chat_B")

            calls: list[tuple[str, str]] = []
            original = store.rebuild_bank

            def spy(category, namespace=""):
                calls.append((namespace, category))
                return original(category, namespace)

            with patch.object(store, "rebuild_bank", side_effect=spy):
                processed = store.rebuild_all_vectors(namespace="chat_A")

            assert processed == 1
            assert calls == [("chat_A", "general")]
        finally:
            store.close()


class TestRebuildStability:
    def test_rebuild_preserves_fact_count_and_dimension(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("A fact.", category="general", namespace="chat_A")
            store.add_fact("Another fact.", category="general", namespace="chat_A")

            row1 = _bank_row(store, bank_name("chat_A", "general"))
            store.rebuild_bank("general", "chat_A")
            row2 = _bank_row(store, bank_name("chat_A", "general"))

            assert row1["fact_count"] == row2["fact_count"] == 2
            assert row1["dim"] == row2["dim"] == 32
        finally:
            store.close()

    def test_dimension_change_rebuilds_vectors_and_banks_consistently(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("A fact.", category="general", namespace="chat_A")
            store.add_fact("Another fact.", category="project", namespace="chat_B")

            store.rebuild_all_vectors(dim=64)

            assert store.hrr_dim == 64
            row_a = _bank_row(store, bank_name("chat_A", "general"))
            row_b = _bank_row(store, bank_name("chat_B", "project"))
            assert row_a["dim"] == 64
            assert row_b["dim"] == 64
            assert len(row_a["vector"]) == 64 * 8
            assert len(row_b["vector"]) == 64 * 8
        finally:
            store.close()


class TestScopedRetrievalNeverUsesLegacyBank:
    def test_scoped_probe_ignores_preexisting_legacy_global_bank(self, tmp_path, numpy_enabled):
        """A deliberately pre-existing legacy cross-namespace `cat:<category>`
        bank must never be consulted to serve a scoped (non-empty-namespace)
        query. We assert this behaviorally: probing a namespace that has no
        bank of its own must produce IDENTICAL results whether or not a
        legacy global bank with unrelated content happens to exist — if the
        legacy bank were (incorrectly) consulted, the bank-vector scoring
        branch would run instead of the per-fact fallback branch and the
        scores would differ.
        """
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            # Facts belong ONLY to the scoped namespace under test; this
            # namespace never gets its own bank built for "project" because
            # we only add facts under "general" here, then probe "project".
            store.add_fact("Zara works on the project team.", category="project", namespace="chat_A")

            retriever = FactRetriever(store, hrr_dim=32)
            baseline = retriever.probe("Zara", category="project", namespace="chat_A")

            # Now inject a legacy cross-namespace aggregate bank directly —
            # simulating a pre-Phase-2 database that still has one lying
            # around (Phase 2 never deletes it implicitly).
            store._conn.execute(
                "INSERT INTO memory_banks (bank_name, vector, dim, fact_count) VALUES (?, ?, ?, ?)",
                ("cat:project", b"\x00" * (32 * 8), 32, 1),
            )
            store._conn.commit()

            after = retriever.probe("Zara", category="project", namespace="chat_A")

            assert [f["fact_id"] for f in baseline] == [f["fact_id"] for f in after]
            assert [round(f["score"], 9) for f in baseline] == [round(f["score"], 9) for f in after]
        finally:
            store.close()

    def test_unscoped_probe_still_uses_unscoped_bank(self, tmp_path, numpy_enabled):
        """Backward-compat: namespace="" (CLI) continues to use the
        cat:<category> bank identity, unaffected by Phase 2."""
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("Zara works on the project team.", category="project", namespace="")
            assert _bank_row(store, "cat:project") is not None

            retriever = FactRetriever(store, hrr_dim=32)
            results = retriever.probe("Zara", category="project", namespace="")
            assert len(results) == 1
        finally:
            store.close()


class TestNoNumpyDegradation:
    def test_fts_fallback_works_without_numpy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hrr, "_HAS_NUMPY", False)
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            store.add_fact("Zara works on the project team.", category="project", namespace="chat_A")
            store.add_fact("Other namespace fact.", category="project", namespace="chat_B")

            # Bank rebuild is a no-op without numpy — must not raise, and
            # must leave memory_banks empty.
            assert store._conn.execute("SELECT COUNT(*) FROM memory_banks").fetchone()[0] == 0

            retriever = FactRetriever(store, hrr_dim=32)
            results = retriever.probe("Zara", category="project", namespace="chat_A")
            assert len(results) == 1
            assert results[0]["content"] == "Zara works on the project team."

            # rebuild_all_vectors / rebuild_bank must not raise either.
            assert store.rebuild_all_vectors() == 0
            store.rebuild_bank("project", "chat_A")
        finally:
            store.close()
