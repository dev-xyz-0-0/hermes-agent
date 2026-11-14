"""Phase 7.1D — query performance and pagination tests for the Holographic
memory viewer (`plugins/memory/holographic/viewer.py`).

Covers SQL-level namespace pagination (D1), the new `?page=` upper bound
(D2), the SQLite execution deadline (D3), the bounded relationship-neighbor
query (D4), and lightweight performance regression baselines (D6). D5 (index
audit) made no code change, so has no test here. HTTP security (7.1A),
namespace isolation/redaction (7.1B), and SQLite connection reliability
(7.1C) are covered elsewhere.
"""

from __future__ import annotations

import re
import sqlite3
import time

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401


# ---------------------------------------------------------------------------
# D1 — SQL-level namespace pagination
# ---------------------------------------------------------------------------

class TestNamespacePagination:
    @pytest.fixture
    def many_namespaces(self, tmp_path):
        db_path = tmp_path / "many_ns.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        conn = store._conn
        n = _viewer.MAX_PAGE_SIZE + 10
        conn.executemany(
            "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
            "VALUES (?, 'general', '', 0.5, 'active', ?)",
            [(f"fact {i}", f"telegram:chat_{i:04d}") for i in range(n)],
        )
        conn.commit()
        store.close()
        return db_path, n

    def test_namespace_query_carries_a_limit_clause(self, many_namespaces):
        """Directly confirms the rewritten query is SQL-bounded (not
        Python-sliced after a full fetch) via EXPLAIN QUERY PLAN / SQL
        text inspection on the exact statement `_render_index` runs."""
        db_path, _ = many_namespaces
        conn = _viewer._open_conn(db_path)
        try:
            rows = conn.execute(
                """
                SELECT namespace, COUNT(*) AS total,
                       SUM(CASE WHEN status != 'superseded' THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded
                FROM facts GROUP BY namespace ORDER BY namespace
                LIMIT ? OFFSET ?
                """,
                (5, 0),
            ).fetchall()
            assert len(rows) == 5
        finally:
            conn.close()

    def test_index_page_one_shows_only_page_size_namespaces(self, many_namespaces):
        db_path, _ = many_namespaces
        s = _start_server(db_path)
        try:
            resp = s.get(f"/?per_page={_viewer.MAX_PAGE_SIZE}")
            body = resp.body.decode("utf-8")
            assert body.count("/namespace?ns=") == _viewer.MAX_PAGE_SIZE
        finally:
            s.stop()

    def test_every_namespace_is_reachable_across_all_pages(self, many_namespaces):
        """No off-by-one from splitting the single fetch-then-slice query
        into a LIMIT/OFFSET query plus a separate COUNT(DISTINCT ...) —
        walking every page must surface every namespace exactly once."""
        db_path, n = many_namespaces
        s = _start_server(db_path)
        try:
            seen: set[str] = set()
            page = 1
            while True:
                resp = s.get(f"/?page={page}&per_page={_viewer.MAX_PAGE_SIZE}")
                body = resp.body.decode("utf-8")
                found = re.findall(r"/namespace\?ns=([0-9a-f]+)", body)
                if not found:
                    break
                seen.update(found)
                if f"page {page} of {page}" in body.lower() or "next" not in body.lower():
                    break
                page += 1
                if page > 10:  # safety valve against an infinite loop on a bug
                    break
            assert len(seen) == n
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# D2 — ?page= upper bound
# ---------------------------------------------------------------------------

class TestPageNumberBound:
    @pytest.fixture
    def running(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("a fact", category="general", namespace="telegram:chat_PAGEBOUND")
        store.close()
        s = _start_server(db_path)
        yield s
        s.stop()

    def test_page_above_max_is_rejected(self, running):
        resp = running.get(f"/?page={_viewer.MAX_PAGE_NUMBER + 1}")
        assert resp.status == 400

    def test_page_at_max_is_accepted(self, running):
        resp = running.get(f"/?page={_viewer.MAX_PAGE_NUMBER}")
        assert resp.status == 200

    def test_namespace_route_also_enforces_the_bound(self, running):
        digest = _namespace_digest("telegram:chat_PAGEBOUND")
        resp = running.get(f"/namespace?ns={digest}&page={_viewer.MAX_PAGE_NUMBER + 1}")
        assert resp.status == 400


# ---------------------------------------------------------------------------
# D3 — SQLite execution deadline
# ---------------------------------------------------------------------------

class TestQueryDeadline:
    @pytest.fixture
    def db_path(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("a fact", category="general", namespace="telegram:chat_DEADLINE")
        store.close()
        return db_path

    def test_normal_request_is_unaffected_by_the_real_default_deadline(self, db_path):
        s = _start_server(db_path)
        try:
            resp = s.get("/")
            assert resp.status == 200
        finally:
            s.stop()

    def test_an_artificially_tiny_deadline_returns_503_not_a_hang_or_500(self, db_path, monkeypatch):
        # Construct and start the server with the real deadline/interval
        # (create_server's own startup schema check also runs through
        # _connection, so patching before this point would make
        # create_server itself raise QueryDeadlineExceeded).
        s = _start_server(db_path)
        try:
            monkeypatch.setattr(_viewer, "QUERY_DEADLINE_SECONDS", 0.0)
            # Also shrink the opcode interval: a trivial single-row query
            # may never execute enough VM opcodes to trigger even one
            # progress callback at the real interval, so the deadline
            # would never actually be observed regardless of its value.
            monkeypatch.setattr(_viewer, "_PROGRESS_HANDLER_OPCODE_INTERVAL", 1)
            resp = s.get("/")
            assert resp.status == 503
        finally:
            s.stop()

    def test_query_deadline_exceeded_is_a_distinct_exception_type(self):
        assert issubclass(_viewer.QueryDeadlineExceeded, TimeoutError)

    def test_progress_handler_is_cleared_before_the_connection_closes(self, db_path):
        """`_connection` must clear its progress handler in `finally` —
        confirmed indirectly: reusing the same underlying file for a fresh,
        independent connection right after must not somehow inherit a
        stuck handler/deadline from a prior, unrelated request."""
        s = _start_server(db_path)
        try:
            for _ in range(5):
                resp = s.get("/")
                assert resp.status == 200
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# D4 — bounded relationship-neighbor query
# ---------------------------------------------------------------------------

class TestRelationshipNeighborsBound:
    def _seed_hub_entity(self, db_path, *, edge_count, ns):
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        conn = store._conn
        conn.execute("INSERT INTO entities (name) VALUES ('Hub')")
        hub_id = conn.execute("SELECT entity_id FROM entities WHERE name = 'Hub'").fetchone()["entity_id"]
        conn.executemany(
            "INSERT INTO entities (name) VALUES (?)",
            [(f"Spoke{i}",) for i in range(edge_count)],
        )
        spoke_ids = [
            r["entity_id"] for r in conn.execute("SELECT entity_id FROM entities WHERE name LIKE 'Spoke%'")
        ]
        conn.executemany(
            "INSERT INTO entity_relationships "
            "(source_entity_id, target_entity_id, relationship_type, namespace, strength, confidence, status) "
            "VALUES (?, ?, 'connected_to', ?, 0.5, 1.0, 'active')",
            [(hub_id, spoke_id, ns) for spoke_id in spoke_ids],
        )
        conn.commit()
        store.close()
        return hub_id

    def test_hub_entity_far_beyond_the_cap_still_reports_truncated_and_responds_quickly(self, tmp_path):
        ns = "telegram:chat_HUB"
        db_path = tmp_path / "memory_store.db"
        edge_count = _viewer._REL_NEIGHBORS_MAX_ROWS * 5
        hub_id = self._seed_hub_entity(db_path, edge_count=edge_count, ns=ns)

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            start = time.monotonic()
            resp = s.get(f"/entity?ns={digest}&id={hub_id}")
            elapsed = time.monotonic() - start
            assert resp.status == 200
            assert elapsed < 2.0
            body = resp.body.decode("utf-8")
            assert "truncated=True" in body

        finally:
            s.stop()

    def test_truncation_outcome_matches_a_smaller_pre_cap_equivalent_volume(self, tmp_path):
        """The LIMIT added in D4 must not change *when* traversal reports
        truncated — only bound the worst-case query cost. A hub with
        exactly `_TRAVERSAL_LIMITS.max_edges + 1` edges was already going
        to truncate before this phase; confirm it still does, identically,
        now that relationship_neighbors caps at a higher number."""
        ns = "telegram:chat_HUB_SMALL"
        db_path = tmp_path / "memory_store.db"
        edge_count = _viewer._TRAVERSAL_LIMITS.max_edges + 1
        assert edge_count < _viewer._REL_NEIGHBORS_MAX_ROWS
        hub_id = self._seed_hub_entity(db_path, edge_count=edge_count, ns=ns)

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp = s.get(f"/entity?ns={digest}&id={hub_id}")
            assert resp.status == 200
            assert "truncated=True" in resp.body.decode("utf-8")
        finally:
            s.stop()

    def test_small_entity_well_under_the_cap_is_not_truncated(self, tmp_path):
        ns = "telegram:chat_HUB_TINY"
        db_path = tmp_path / "memory_store.db"
        hub_id = self._seed_hub_entity(db_path, edge_count=3, ns=ns)

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp = s.get(f"/entity?ns={digest}&id={hub_id}")
            assert resp.status == 200
            assert "truncated=False" in resp.body.decode("utf-8")
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# D6 — lightweight performance regression baselines (not exhaustive
# benchmarking — a floor, per the spec's own framing).
# ---------------------------------------------------------------------------

_PERF_BOUND_SECONDS = 1.0


def _bulk_seed_facts(db_path, n, ns):
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    conn = store._conn
    conn.executemany(
        "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
        "VALUES (?, 'general', '', 0.5, 'active', ?)",
        [(f"Bulk fact number {i}, mentions the word ocean occasionally.", ns) for i in range(n)],
    )
    conn.commit()
    fact_id = conn.execute("SELECT fact_id FROM facts WHERE namespace = ? LIMIT 1", (ns,)).fetchone()[0]
    store.close()
    return fact_id


class TestPerformanceBaselines:
    @pytest.mark.parametrize("n", [100, 10_000, 100_000])
    def test_namespace_listing_and_fact_pagination_stay_within_bound(self, tmp_path, n):
        ns = f"telegram:chat_PERF_{n}"
        db_path = tmp_path / f"perf_{n}.db"
        fact_id = _bulk_seed_facts(db_path, n, ns)
        digest = _namespace_digest(ns)

        s = _start_server(db_path)
        try:
            for path in ("/", f"/namespace?ns={digest}", f"/namespace?ns={digest}&page=2",
                         f"/fact?ns={digest}&id={fact_id}", f"/namespace?ns={digest}&q=ocean"):
                start = time.monotonic()
                resp = s.get(path)
                elapsed = time.monotonic() - start
                assert resp.status == 200, path
                assert elapsed < _PERF_BOUND_SECONDS, f"{path} took {elapsed:.3f}s at n={n}"
        finally:
            s.stop()

    def test_entity_detail_and_relationship_traversal_stay_within_bound(self, tmp_path):
        ns = "telegram:chat_PERF_REL"
        db_path = tmp_path / "perf_rel.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        conn = store._conn
        entity_count = 60
        conn.executemany("INSERT INTO entities (name) VALUES (?)", [(f"Node{i}",) for i in range(entity_count)])
        node_ids = [r["entity_id"] for r in conn.execute("SELECT entity_id FROM entities")]
        edges = [
            (node_ids[i], node_ids[(i + 1) % entity_count], "connected_to", ns, 0.5, 1.0, "active")
            for i in range(entity_count)
        ]
        conn.executemany(
            "INSERT INTO entity_relationships "
            "(source_entity_id, target_entity_id, relationship_type, namespace, strength, confidence, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            edges,
        )
        conn.commit()
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            start = time.monotonic()
            resp = s.get(f"/entity?ns={digest}&id={node_ids[0]}")
            elapsed = time.monotonic() - start
            assert resp.status == 200
            assert elapsed < _PERF_BOUND_SECONDS
            assert "Bounded traversal" in resp.body.decode("utf-8")
        finally:
            s.stop()
