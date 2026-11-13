"""Phase 5 — namespace-scoped entity relationship tests.

See docs/plans/demo-reference-plan/checklist.md section 8 ("Phase 5 —
Scoped relationships" and its "Relationship tests" subsection) and
implementation-plan.md "Phase 5 — Namespace-scoped entity relationships" /
"9.6 Relationship tests".
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from plugins.memory.holographic import relationships as rel
from plugins.memory.holographic.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
    yield s
    s.close()


def _entity(store: MemoryStore, name: str) -> int:
    """Resolve (or create) a global entity row by name — entities are
    shared/global, never namespace-scoped (see store._resolve_entity)."""
    return store._resolve_entity(name)


class TestNamespaceScopedUniqueness:
    def test_same_edge_in_two_namespaces_are_independent_rows(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")

        id_a = rel.upsert_relationship(store, alice, bob, "manages", namespace="chat_a")
        id_b = rel.upsert_relationship(store, alice, bob, "manages", namespace="chat_b")

        assert id_a != id_b
        rows = store.read("SELECT COUNT(*) FROM entity_relationships WHERE source_entity_id = ? AND target_entity_id = ?", (alice, bob))
        assert rows[0][0] == 2

        edges_a = rel.list_relationships(store, alice, namespace="chat_a")
        edges_b = rel.list_relationships(store, alice, namespace="chat_b")
        assert len(edges_a) == 1 and len(edges_b) == 1
        assert edges_a[0].namespace == "chat_a"
        assert edges_b[0].namespace == "chat_b"

    def test_identical_entity_names_in_two_chats_do_not_leak_edges(self, store):
        """The single highest-risk bug this phase could introduce: entities
        are shared/global by NAME (store._resolve_entity), so "Alice" and
        "Bob" resolve to the SAME entity_id regardless of which chat
        mentioned them. Relationship queries must filter by the
        relationship's own namespace column, never infer a cross-chat edge
        just because the entity IDs match."""
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        carol = _entity(store, "Carol")
        assert alice == _entity(store, "Alice")  # same global entity_id, confirmed

        rel.upsert_relationship(store, alice, bob, "manages", namespace="chat_a")
        rel.upsert_relationship(store, alice, carol, "manages", namespace="chat_b")

        edges_a = rel.list_relationships(store, alice, namespace="chat_a")
        edges_b = rel.list_relationships(store, alice, namespace="chat_b")

        assert {e.target_entity_id for e in edges_a} == {bob}
        assert {e.target_entity_id for e in edges_b} == {carol}

        traversal_a = rel.traverse(store, alice, namespace="chat_a")
        traversal_b = rel.traverse(store, alice, namespace="chat_b")
        assert traversal_a.nodes == {alice, bob}
        assert traversal_b.nodes == {alice, carol}

    def test_direction_and_type_are_preserved(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        rel.upsert_relationship(store, alice, bob, "manages", namespace="ns")
        rel.upsert_relationship(store, bob, alice, "reports_to", namespace="ns")

        outgoing_from_alice = rel.list_relationships(store, alice, namespace="ns", direction="outgoing")
        assert len(outgoing_from_alice) == 1
        assert outgoing_from_alice[0].source_entity_id == alice
        assert outgoing_from_alice[0].target_entity_id == bob
        assert outgoing_from_alice[0].relationship_type == "manages"

        incoming_to_alice = rel.list_relationships(store, alice, namespace="ns", direction="incoming")
        assert len(incoming_to_alice) == 1
        assert incoming_to_alice[0].source_entity_id == bob
        assert incoming_to_alice[0].relationship_type == "reports_to"

        # A->B "manages" is never returned as B->A, nor as a different type.
        both = rel.list_relationships(store, alice, namespace="ns", direction="both")
        types_by_direction = {(e.source_entity_id, e.target_entity_id): e.relationship_type for e in both}
        assert types_by_direction[(alice, bob)] == "manages"
        assert types_by_direction[(bob, alice)] == "reports_to"


class TestUpsert:
    def test_upsert_does_not_duplicate_edges(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        id1 = rel.upsert_relationship(store, alice, bob, "manages", namespace="ns", strength=0.4, confidence=0.5)
        id2 = rel.upsert_relationship(store, alice, bob, "manages", namespace="ns", strength=0.9, confidence=0.8)

        assert id1 == id2
        count = store.read(
            "SELECT COUNT(*) FROM entity_relationships WHERE namespace='ns' AND source_entity_id=? AND target_entity_id=? AND relationship_type='manages'",
            (alice, bob),
        )[0][0]
        assert count == 1

        row = store.read(
            "SELECT strength, confidence FROM entity_relationships WHERE relationship_id = ?", (id1,)
        )[0]
        assert row["strength"] == pytest.approx(0.9)
        assert row["confidence"] == pytest.approx(0.8)

    def test_strength_and_confidence_are_clamped(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        rid = rel.upsert_relationship(store, alice, bob, "manages", namespace="ns", strength=5.0, confidence=-3.0)
        row = store.read("SELECT strength, confidence FROM entity_relationships WHERE relationship_id = ?", (rid,))[0]
        assert row["strength"] == 1.0
        assert row["confidence"] == 0.0

    def test_remove_relationship(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        rel.upsert_relationship(store, alice, bob, "manages", namespace="ns")
        assert rel.remove_relationship(store, alice, bob, "manages", namespace="ns") is True
        assert rel.list_relationships(store, alice, namespace="ns") == []
        # Removing again is a safe no-op.
        assert rel.remove_relationship(store, alice, bob, "manages", namespace="ns") is False


class TestBoundedTraversal:
    def _chain(self, store, namespace: str, length: int) -> list[int]:
        """entity_0 -> entity_1 -> ... -> entity_{length-1}, one 'manages' edge each."""
        ids = [_entity(store, f"Node{i}") for i in range(length)]
        for i in range(length - 1):
            rel.upsert_relationship(store, ids[i], ids[i + 1], "manages", namespace=namespace)
        return ids

    def test_traversal_respects_max_depth(self, store):
        ids = self._chain(store, "ns", length=6)  # 5 edges deep
        result = rel.traverse(store, ids[0], namespace="ns", limits=rel.TraversalLimits(max_depth=2, max_nodes=100, max_edges=100, deadline_seconds=5))
        # depth=2 reaches ids[0], ids[1] (depth1), ids[2] (depth2) — not ids[3].
        assert ids[3] not in result.nodes
        assert ids[2] in result.nodes
        assert result.truncated is True
        assert result.truncated_reason == "max_depth"

    def test_traversal_respects_max_nodes(self, store):
        # A star: center connects to 10 distinct nodes (one edge each).
        center = _entity(store, "Center")
        leaves = [_entity(store, f"Leaf{i}") for i in range(10)]
        for leaf in leaves:
            rel.upsert_relationship(store, center, leaf, "manages", namespace="ns")

        result = rel.traverse(
            store, center, namespace="ns",
            limits=rel.TraversalLimits(max_depth=5, max_nodes=4, max_edges=100, deadline_seconds=5),
        )
        assert len(result.nodes) <= 4
        assert result.truncated is True
        assert result.truncated_reason == "max_nodes"

    def test_traversal_respects_max_edges(self, store):
        center = _entity(store, "Center2")
        leaves = [_entity(store, f"Leaf2_{i}") for i in range(10)]
        for leaf in leaves:
            rel.upsert_relationship(store, center, leaf, "manages", namespace="ns")

        result = rel.traverse(
            store, center, namespace="ns",
            limits=rel.TraversalLimits(max_depth=5, max_nodes=100, max_edges=3, deadline_seconds=5),
        )
        assert len(result.edges) <= 3
        assert result.truncated is True
        assert result.truncated_reason == "max_edges"

    def test_traversal_respects_deadline(self, store, monkeypatch):
        ids = self._chain(store, "ns", length=5)

        real_monotonic = time.monotonic
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            # First call establishes the deadline (near "now"); every call
            # after that reports far in the future, so the very first
            # in-loop deadline check trips.
            if calls["n"] <= 1:
                return real_monotonic()
            return real_monotonic() + 999

        monkeypatch.setattr(rel.time, "monotonic", fake_monotonic)
        result = rel.traverse(
            store, ids[0], namespace="ns",
            limits=rel.TraversalLimits(max_depth=5, max_nodes=100, max_edges=100, deadline_seconds=1.0),
        )
        assert result.truncated is True
        assert result.truncated_reason == "deadline"

    def test_cycle_terminates_safely(self, store):
        a = _entity(store, "CycleA")
        b = _entity(store, "CycleB")
        c = _entity(store, "CycleC")
        rel.upsert_relationship(store, a, b, "manages", namespace="ns")
        rel.upsert_relationship(store, b, c, "manages", namespace="ns")
        rel.upsert_relationship(store, c, a, "manages", namespace="ns")  # closes the cycle

        started = time.monotonic()
        result = rel.traverse(
            store, a, namespace="ns",
            limits=rel.TraversalLimits(max_depth=50, max_nodes=50, max_edges=50, deadline_seconds=5),
        )
        elapsed = time.monotonic() - started
        assert elapsed < 5  # safety-net timeout guard, not the production limit itself
        assert result.nodes == {a, b, c}
        assert len(result.edges) == 3
        # Not marked truncated — the cycle's visited-set naturally exhausts
        # the frontier well inside every bound.
        assert result.truncated is False


class TestInvalidationPolicy:
    """Documented invalidation policy (relationships.py module docstring):
    forgetting the evidence fact HARD-DELETES the derived relationship
    (irreversible, mirroring remove_fact's own tombstone policy); superseding
    the evidence fact marks the relationship 'stale' so it stops appearing
    in the default (status='active') list/traversal results, without
    deleting the row."""

    def test_forget_invalidates_derived_edge(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        fact_id = store.add_fact("Alice manages Bob.", category="general", namespace="ns")
        rel.upsert_relationship(store, alice, bob, "manages", namespace="ns", source_fact_id=fact_id)
        assert len(rel.list_relationships(store, alice, namespace="ns")) == 1

        # Ordering matters: entity_relationships.source_fact_id/target_fact_id
        # carry a foreign key into facts(fact_id), so the relationship must be
        # invalidated BEFORE the fact row is deleted, not after — deleting the
        # fact first would raise a foreign-key violation. This is exactly the
        # "call this alongside remove_fact" contract documented on
        # `invalidate_for_forgotten_fact`.
        removed = rel.invalidate_for_forgotten_fact(store, fact_id, namespace="ns")
        store.remove_fact(fact_id, namespace="ns", tombstone=True)

        assert removed == 1
        assert rel.list_relationships(store, alice, namespace="ns") == []
        remaining = store.read("SELECT COUNT(*) FROM entity_relationships")[0][0]
        assert remaining == 0

    def test_superseded_fact_does_not_contribute_stale_active_edge(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        old_fact_id = store.add_fact("Alice manages Bob.", category="general", namespace="ns")
        rel.upsert_relationship(store, alice, bob, "manages", namespace="ns", source_fact_id=old_fact_id)

        store.supersede_fact(old_fact_id, "Alice no longer manages Bob.", category="general", namespace="ns")
        marked = rel.mark_stale_for_superseded_fact(store, old_fact_id, namespace="ns")

        assert marked == 1
        # Default list/traversal (status='active') no longer surfaces it.
        assert rel.list_relationships(store, alice, namespace="ns") == []
        # The row itself still exists (not hard-deleted) — inspectable directly.
        row = store.read(
            "SELECT status FROM entity_relationships WHERE source_entity_id=? AND target_entity_id=?",
            (alice, bob),
        )[0]
        assert row["status"] == "stale"


class TestSharedTraversal:
    def test_shared_traversal_follows_enablement_and_allowlist(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        carol = _entity(store, "Carol")

        rel.upsert_relationship(store, alice, bob, "manages", namespace=rel.SHARED_NAMESPACE)
        rel.upsert_relationship(store, alice, carol, "mentors", namespace=rel.SHARED_NAMESPACE)

        # Not allowlisted ("manages" not in shared_relationship_types) -> excluded.
        not_allowlisted = rel.list_relationships(
            store, alice, namespace="chat_a", include_shared=True,
            shared_relationship_types=frozenset({"mentors"}),
        )
        assert {e.relationship_type for e in not_allowlisted} == {"mentors"}

        # include_shared=False (mirrors shared_memory_enabled=False) -> none at all.
        disabled = rel.list_relationships(
            store, alice, namespace="chat_a", include_shared=False,
            shared_relationship_types=frozenset({"mentors", "manages"}),
        )
        assert disabled == []

        # Enabled + allowlisted -> both show up, flagged shared=True, own
        # namespace (empty here) still has priority/is queried too.
        enabled = rel.list_relationships(
            store, alice, namespace="chat_a", include_shared=True,
            shared_relationship_types=frozenset({"mentors", "manages"}),
        )
        assert {e.relationship_type for e in enabled} == {"mentors", "manages"}
        assert all(e.shared for e in enabled)

    def test_shared_traversal_in_bfs(self, store):
        alice = _entity(store, "Alice")
        bob = _entity(store, "Bob")
        rel.upsert_relationship(store, alice, bob, "mentors", namespace=rel.SHARED_NAMESPACE)

        blocked = rel.traverse(store, alice, namespace="chat_a", include_shared=False)
        assert blocked.nodes == {alice}

        allowed = rel.traverse(
            store, alice, namespace="chat_a", include_shared=True,
            shared_relationship_types=frozenset({"mentors"}),
        )
        assert bob in allowed.nodes


class TestLegacyMigration:
    def test_migration_does_not_broadcast_legacy_unscoped_edges(self, tmp_path):
        """Simulate a differently-shaped pre-existing entity_relationships
        table (no namespace column) being opened by the current MemoryStore.
        Per implementation-plan.md's migration plan: legacy rows become
        unscoped (namespace='') — they must NOT be copied into every chat
        namespace, and a scoped traversal/list must not see them."""
        db_path = tmp_path / "legacy.db"

        # Build a fully current-schema database first (so facts/entities
        # have every column `_SCHEMA` expects), then replace
        # entity_relationships with a differently-shaped, pre-existing-style
        # table (no namespace column) to simulate the scenario this
        # migration guards against.
        bootstrap = MemoryStore(db_path=db_path, hrr_dim=32)
        alice_id = bootstrap._resolve_entity("LegacyAlice")
        bob_id = bootstrap._resolve_entity("LegacyBob")
        bootstrap._conn.commit()
        bootstrap.close()

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP TABLE entity_relationships")
            conn.execute(
                """
                CREATE TABLE entity_relationships (
                    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_entity_id INTEGER NOT NULL,
                    target_entity_id INTEGER NOT NULL,
                    relationship_type TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO entity_relationships (source_entity_id, target_entity_id, relationship_type) "
                "VALUES (?, ?, 'manages')",
                (alice_id, bob_id),
            )
            conn.commit()
        finally:
            conn.close()

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(entity_relationships)").fetchall()}
            assert "namespace" in columns
            legacy_row = store.read(
                "SELECT namespace FROM entity_relationships WHERE relationship_id = 1"
            )[0]
            assert legacy_row["namespace"] == ""  # unscoped, not broadcast anywhere

            # A real chat namespace must not see it.
            scoped = rel.list_relationships(store, alice_id, namespace="telegram:chat_X")
            assert scoped == []
            # Only the unscoped ("") namespace sees the legacy row.
            unscoped = rel.list_relationships(store, alice_id, namespace="")
            assert len(unscoped) == 1
        finally:
            store.close()
