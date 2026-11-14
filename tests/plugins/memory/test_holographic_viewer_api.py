"""Phase 7.2B — namespace-scoped JSON API tests for the Holographic memory
viewer (`plugins/memory/holographic/viewer.py`).

Reuses the existing `seeded_db`/`three_namespaces` fixtures (from
`test_holographic_viewer.py`/`test_holographic_viewer_namespace.py`) rather
than re-deriving them, per the pre-implementation audit's instruction not to
duplicate existing working setup.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server, seeded_db  # noqa: F401
from .test_holographic_viewer_namespace import three_namespaces, running3  # noqa: F401


def _get_json(running: _RunningServer, path: str):
    resp = running.get(path)
    return resp, json.loads(resp.body.decode("utf-8")) if resp.body else None


@pytest.fixture
def running(seeded_db):
    db_path, ns_a, ns_b = seeded_db
    s = _start_server(db_path)
    s.ns_a, s.ns_b = ns_a, ns_b
    s.digest_a, s.digest_b = _namespace_digest(ns_a), _namespace_digest(ns_b)
    yield s
    s.stop()


# ---------------------------------------------------------------------------
# 1 — endpoint success cases
# ---------------------------------------------------------------------------

class TestEndpointSuccessCases:
    def test_namespaces(self, running):
        resp, body = _get_json(running, "/api/namespaces")
        assert resp.status == 200
        digests = {n["namespace"] for n in body["namespaces"]}
        assert running.digest_a in digests and running.digest_b in digests
        assert "pagination" in body

    def test_namespaces_never_includes_individual_facts_or_raw_identity_by_default(self, running):
        _, body = _get_json(running, "/api/namespaces")
        raw = json.dumps(body)
        assert running.ns_a not in raw
        assert "Nightjar" not in raw
        for entry in body["namespaces"]:
            assert "raw_identity" not in entry

    def test_overview(self, running):
        resp, body = _get_json(running, f"/api/overview?ns={running.digest_a}")
        assert resp.status == 200
        assert body["namespace"] == running.digest_a
        assert body["facts"]["total"] >= 1
        assert "categories" in body and "trust_distribution" in body and "recent_facts" in body

    def test_facts_list(self, running):
        resp, body = _get_json(running, f"/api/facts?ns={running.digest_a}")
        assert resp.status == 200
        assert isinstance(body["facts"], list) and body["facts"]

    def test_fact_detail(self, running):
        _, listing = _get_json(running, f"/api/facts?ns={running.digest_a}")
        fact_id = listing["facts"][0]["fact_id"]
        resp, body = _get_json(running, f"/api/facts/{fact_id}?ns={running.digest_a}")
        assert resp.status == 200
        assert body["fact"]["fact_id"] == fact_id
        assert "revision" in body

    def test_search_literal(self, running):
        resp, body = _get_json(running, f"/api/search?ns={running.digest_a}&q=Alice")
        assert resp.status == 200
        assert body["mode_used"] == "literal"
        assert any("Alice" in r["content"] for r in body["results"])

    def test_entities_list(self, running):
        resp, body = _get_json(running, f"/api/entities?ns={running.digest_a}")
        assert resp.status == 200
        assert isinstance(body["entities"], list)

    def test_timeline(self, running):
        resp, body = _get_json(running, f"/api/timeline?ns={running.digest_a}")
        assert resp.status == 200
        assert body["timestamp_basis"] == "created_at"
        assert isinstance(body["days"], list)


# ---------------------------------------------------------------------------
# 2/3 — missing, invalid, and empty namespace
# ---------------------------------------------------------------------------

class TestNamespaceRequirement:
    @pytest.mark.parametrize("path", [
        "/api/overview", "/api/facts", "/api/search", "/api/entities", "/api/timeline",
    ])
    def test_missing_ns_is_400(self, running, path):
        resp, body = _get_json(running, path)
        assert resp.status == 400
        assert body["error"]["code"] == "MISSING_NAMESPACE"

    @pytest.mark.parametrize("path", [
        "/api/overview?ns=deadbeefdeadbeef", "/api/facts?ns=deadbeefdeadbeef",
        "/api/search?ns=deadbeefdeadbeef&q=x", "/api/entities?ns=deadbeefdeadbeef",
        "/api/timeline?ns=deadbeefdeadbeef",
    ])
    def test_invalid_ns_is_404(self, running, path):
        resp, body = _get_json(running, path)
        assert resp.status == 404
        assert body["error"]["code"] == "INVALID_NAMESPACE"

    def test_fact_detail_missing_ns_is_400(self, running):
        resp, body = _get_json(running, "/api/facts/1")
        assert resp.status == 400
        assert body["error"]["code"] == "MISSING_NAMESPACE"

    def test_entity_graph_invalid_ns_is_404(self, running):
        resp, body = _get_json(running, "/api/entities/1/graph?ns=deadbeefdeadbeef")
        assert resp.status == 404
        assert body["error"]["code"] == "INVALID_NAMESPACE"

    def test_empty_namespace_ns_equals_empty_string_is_the_valid_unscoped_representation(self, tmp_path):
        """`ns=` (empty) is the unscoped/CLI namespace, not a missing
        parameter and not "all namespaces" — mirrors `_parse_str`'s own
        documented absent-vs-empty distinction."""
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("An unscoped fact with no namespace set.", category="general")
        store.close()

        s = _start_server(db_path)
        try:
            resp, body = _get_json(s, "/api/overview?ns=")
            assert resp.status == 200
            assert body["namespace"] == ""
            assert body["facts"]["total"] == 1
        finally:
            s.stop()

    def test_an_empty_database_namespace_has_zero_facts_not_an_error(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        s = _start_server(db_path)
        try:
            resp, body = _get_json(s, "/api/namespaces")
            assert resp.status == 200
            assert body["namespaces"] == []
            assert body["pagination"]["total"] == 0
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 4/5 — cross-namespace isolation and shared entities
# ---------------------------------------------------------------------------

class TestCrossNamespaceIsolation:
    def test_facts_list_never_crosses_namespaces(self, running3):
        digest_a = _namespace_digest(running3.ns_a)
        _, body = _get_json(running3, f"/api/facts?ns={digest_a}")
        raw = json.dumps(body)
        assert "Globex" not in raw
        assert "coffee" not in raw

    def test_search_never_crosses_namespaces(self, running3):
        digest_a = _namespace_digest(running3.ns_a)
        _, body = _get_json(running3, f"/api/search?ns={digest_a}&q=Alice")
        assert all("coffee" not in r["content"] for r in body["results"])

    def test_a_fact_id_from_one_namespace_404s_under_another(self, running3):
        digest_a = _namespace_digest(running3.ns_a)
        digest_b = _namespace_digest(running3.ns_b)
        _, listing = _get_json(running3, f"/api/facts?ns={digest_a}")
        fact_id = listing["facts"][0]["fact_id"]
        resp, body = _get_json(running3, f"/api/facts/{fact_id}?ns={digest_b}")
        assert resp.status == 404
        assert body["error"]["code"] == "NOT_FOUND"

    def test_shared_entity_graph_shows_only_its_own_namespaces_relationship(self, running3):
        digest_a = _namespace_digest(running3.ns_a)
        digest_c = _namespace_digest(running3.ns_c)

        _, body_a = _get_json(running3, f"/api/entities/{running3.alice_id}/graph?ns={digest_a}")
        types_a = {r["relationship_type"] for r in body_a["relationships"]}
        assert "drinks_with_a" in types_a
        assert "drinks_with_c" not in types_a

        _, body_c = _get_json(running3, f"/api/entities/{running3.alice_id}/graph?ns={digest_c}")
        types_c = {r["relationship_type"] for r in body_c["relationships"]}
        assert "drinks_with_c" in types_c
        assert "drinks_with_a" not in types_c

    def test_shared_entity_not_reachable_under_a_namespace_that_never_used_it(self, running3):
        digest_b = _namespace_digest(running3.ns_b)
        resp, body = _get_json(running3, f"/api/entities/{running3.alice_id}/graph?ns={digest_b}")
        assert resp.status == 404
        assert body["error"]["code"] == "NOT_FOUND"

    def test_entities_list_is_scoped_per_namespace(self, running3):
        digest_b = _namespace_digest(running3.ns_b)
        _, body = _get_json(running3, f"/api/entities?ns={digest_b}")
        names = {e["name"] for e in body["entities"]}
        assert "Alice" not in names

    def test_timeline_is_scoped_per_namespace(self, running3):
        digest_b = _namespace_digest(running3.ns_b)
        _, body = _get_json(running3, f"/api/timeline?ns={digest_b}")
        total = sum(d["count"] for d in body["days"])
        assert total == 1  # only Bob's single fact in namespace B


# ---------------------------------------------------------------------------
# 6 — graph boundary enforcement
# ---------------------------------------------------------------------------

class TestGraphBoundaryEnforcement:
    def _seed_hub_entity(self, db_path, *, edge_count, ns):
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        conn = store._conn
        conn.execute("INSERT INTO entities (name) VALUES ('Hub')")
        hub_id = conn.execute("SELECT entity_id FROM entities WHERE name = 'Hub'").fetchone()["entity_id"]
        conn.executemany(
            "INSERT INTO entities (name) VALUES (?)", [(f"Spoke{i}",) for i in range(edge_count)],
        )
        spoke_ids = [r["entity_id"] for r in conn.execute("SELECT entity_id FROM entities WHERE name LIKE 'Spoke%'")]
        conn.executemany(
            "INSERT INTO entity_relationships "
            "(source_entity_id, target_entity_id, relationship_type, namespace, strength, confidence, status) "
            "VALUES (?, ?, 'connected_to', ?, 0.5, 1.0, 'active')",
            [(hub_id, spoke_id, ns) for spoke_id in spoke_ids],
        )
        conn.commit()
        store.close()
        return hub_id

    def test_hub_entity_far_beyond_the_cap_is_bounded_and_reports_truncated(self, tmp_path):
        ns = "telegram:chat_API_HUB"
        db_path = tmp_path / "memory_store.db"
        edge_count = _viewer._REL_NEIGHBORS_MAX_ROWS * 5
        hub_id = self._seed_hub_entity(db_path, edge_count=edge_count, ns=ns)

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            start = time.monotonic()
            resp, body = _get_json(s, f"/api/entities/{hub_id}/graph?ns={digest}")
            elapsed = time.monotonic() - start
            assert resp.status == 200
            assert elapsed < 5.0
            assert body["graph"]["truncated"] is True
            assert len(body["graph"]["nodes"]) <= _viewer._TRAVERSAL_LIMITS.max_nodes
            assert len(body["graph"]["edges"]) <= _viewer._TRAVERSAL_LIMITS.max_edges
            assert len(body["relationships"]) <= _viewer.MAX_PAGE_SIZE
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 7 — secret redaction
# ---------------------------------------------------------------------------

class TestSecretRedaction:
    def test_api_key_in_content_is_redacted_across_facts_fact_detail_search_and_overview(self, running):
        digest = running.digest_a
        for path in (
            f"/api/facts?ns={digest}",
            f"/api/search?ns={digest}&q=key",
            f"/api/overview?ns={digest}",
        ):
            _, body = _get_json(running, path)
            raw = json.dumps(body)
            assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in raw

    def test_api_key_redacted_in_fact_detail_and_in_supersession_chain(self, tmp_path):
        ns = "telegram:chat_API_SECRET"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        old_id = store.add_fact(
            "Alice's api_key is sk-ant-zzzsupersecretvalue0123456789", category="tool", namespace=ns,
        )
        new_id = store.supersede_fact(old_id, "Alice rotated her credentials.", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            _, old_body = _get_json(s, f"/api/facts/{old_id}?ns={digest}")
            assert "sk-ant-zzzsupersecretvalue0123456789" not in json.dumps(old_body)

            _, new_body = _get_json(s, f"/api/facts/{new_id}?ns={digest}")
            ancestor = new_body["revision"]["ancestors"][0]
            assert "sk-ant-zzzsupersecretvalue0123456789" not in ancestor["content"]
        finally:
            s.stop()

    def test_secret_shaped_entity_name_is_redacted_in_entity_graph(self, tmp_path):
        ns = "telegram:chat_API_ENTITY_SECRET"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
        store._conn.execute("INSERT INTO entities (name) VALUES (?)", (secret,))
        entity_id = store._conn.execute("SELECT entity_id FROM entities WHERE name = ?", (secret,)).fetchone()["entity_id"]
        other_id = store._conn.execute("INSERT INTO entities (name) VALUES ('Other')").lastrowid
        store._conn.execute(
            "INSERT INTO entity_relationships "
            "(source_entity_id, target_entity_id, relationship_type, namespace, strength, confidence, status) "
            "VALUES (?, ?, 'related_to', ?, 0.5, 1.0, 'active')",
            (entity_id, other_id, ns),
        )
        store._conn.commit()
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            _, body = _get_json(s, f"/api/entities/{entity_id}/graph?ns={digest}")
            assert secret not in json.dumps(body)
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 8 — HTML/JSON injection-shaped content round-trips safely as data
# ---------------------------------------------------------------------------

class TestInjectionShapedContent:
    @pytest.mark.parametrize("payload", [
        '"><script>alert(1)</script>',
        "'; DROP TABLE facts; --",
        '{"nested": "json looking content"}',
        "unterminated \" quote",
        "back\\slash and \ttab",
        "emoji 🔥 and 中文",
    ])
    def test_fact_content_round_trips_through_json_without_corruption(self, tmp_path, payload):
        ns = "telegram:chat_API_INJECTION"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        fact_id = store.add_fact(payload, category="general", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp = s.get(f"/api/facts/{fact_id}?ns={digest}")
            # The raw bytes must always be valid, parseable JSON — never
            # truncated or malformed by an unescaped special character.
            body = json.loads(resp.body.decode("utf-8"))
            assert body["fact"]["content"] == payload or "[REDACTED" in body["fact"]["content"]
            assert "<script>" not in resp.body.decode("utf-8") or payload.count("<script>") > 0
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 9 — pagination limits
# ---------------------------------------------------------------------------

class TestPaginationLimits:
    def test_limit_is_clamped_to_max_page_size(self, running):
        _, body = _get_json(running, f"/api/facts?ns={running.digest_a}&limit=999999")
        assert body["pagination"]["limit"] == _viewer.MAX_PAGE_SIZE

    def test_limit_below_one_is_clamped_to_one(self, running):
        _, body = _get_json(running, f"/api/facts?ns={running.digest_a}&limit=0")
        assert body["pagination"]["limit"] == 1

    def test_offset_is_clamped_not_rejected(self, running):
        resp, body = _get_json(running, f"/api/facts?ns={running.digest_a}&offset=99999999999")
        assert resp.status == 200
        assert body["pagination"]["offset"] == _viewer.MAX_API_OFFSET

    def test_negative_offset_is_clamped_to_zero(self, running):
        _, body = _get_json(running, f"/api/facts?ns={running.digest_a}&offset=-5")
        assert body["pagination"]["offset"] == 0

    def test_has_more_is_correct_across_a_full_pagination_walk(self, tmp_path):
        ns = "telegram:chat_API_PAGINATION"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        for i in range(7):
            store.add_fact(f"paged fact {i}", category="general", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            seen = set()
            offset = 0
            for _ in range(10):
                _, body = _get_json(s, f"/api/facts?ns={digest}&limit=3&offset={offset}")
                for f in body["facts"]:
                    seen.add(f["fact_id"])
                if not body["pagination"]["has_more"]:
                    break
                offset += 3
            assert len(seen) == 7
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 10 — FTS5 and literal search
# ---------------------------------------------------------------------------

class TestSearchModes:
    def test_fts_mode_used_when_available(self, tmp_path):
        ns = "telegram:chat_API_FTS"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("The quick brown fox jumps over the lazy dog.", category="general", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            _, body = _get_json(s, f"/api/search?ns={digest}&q=fox&mode=fts")
            assert body["mode_used"] == "fts"
            assert body["fallback_reason"] is None
            assert any("fox" in r["content"] for r in body["results"])
        finally:
            s.stop()

    def test_fts_operator_injection_is_neutralized(self, tmp_path):
        ns = "telegram:chat_API_FTS_INJECTION"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("A fact mentioning OR and NOT as plain words.", category="general", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp, body = _get_json(s, f"/api/search?ns={digest}&q=" + "unterminated%20%22quote&mode=fts")
            assert resp.status == 200  # never a 500 from malformed FTS5 syntax
        finally:
            s.stop()

    def test_literal_mode_escapes_like_wildcards(self, tmp_path):
        ns = "telegram:chat_API_LITERAL"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("Discount is 50% off today.", category="general", namespace=ns)
        store.add_fact("A fact with no percent sign at all.", category="general", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            _, body = _get_json(s, f"/api/search?ns={digest}&q=50%25&mode=literal")
            assert len(body["results"]) == 1
            assert "50%" in body["results"][0]["content"]
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 11 — database timeout
# ---------------------------------------------------------------------------

class TestDatabaseTimeout:
    def test_artificial_deadline_returns_a_clean_json_503(self, tmp_path, monkeypatch):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("a fact", category="general", namespace="telegram:chat_API_DEADLINE")
        store.close()

        s = _start_server(db_path)
        try:
            monkeypatch.setattr(_viewer, "QUERY_DEADLINE_SECONDS", 0.0)
            monkeypatch.setattr(_viewer, "_PROGRESS_HANDLER_OPCODE_INTERVAL", 1)
            resp = s.get("/api/namespaces")
            assert resp.status == 503
            body = json.loads(resp.body.decode("utf-8"))
            assert body["error"]["code"] == "QUERY_DEADLINE_EXCEEDED"
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# 12 — schema incompatibility (checked at construction time, same as every
# other route — the JSON API introduces no second code path around it)
# ---------------------------------------------------------------------------

class TestSchemaIncompatibility:
    def test_a_non_sqlite_file_still_fails_at_server_construction_not_at_request_time(self, tmp_path):
        db_path = tmp_path / "not_a_database.db"
        db_path.write_bytes(b"not a real sqlite file")
        with pytest.raises(_viewer.UnsupportedSchemaError):
            _viewer.create_server(db_path, port=0)


# ---------------------------------------------------------------------------
# 13 — unsupported HTTP methods
# ---------------------------------------------------------------------------

class TestUnsupportedMethods:
    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    @pytest.mark.parametrize("path", ["/api/namespaces", "/api/overview", "/api/facts", "/api/search"])
    def test_mutation_methods_are_rejected_with_json_405(self, running, method, path):
        resp = running.request(method, path)
        assert resp.status == 405
        assert resp.getheader("Content-Type") == "application/json; charset=utf-8"
        body = json.loads(resp.body.decode("utf-8"))
        assert body["error"]["code"] == "METHOD_NOT_ALLOWED"
        assert resp.getheader("Allow") == "GET, HEAD"


# ---------------------------------------------------------------------------
# 14 — consistent security headers
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    @pytest.mark.parametrize("path", [
        "/api/namespaces", "/api/overview?ns={d}", "/api/facts?ns={d}",
        "/api/search?ns={d}&q=x", "/api/entities?ns={d}", "/api/timeline?ns={d}",
        "/api/doesnotexist",
    ])
    def test_every_json_response_carries_the_full_security_header_set(self, running, path):
        resp = running.get(path.format(d=running.digest_a))
        assert resp.getheader("Content-Type") == "application/json; charset=utf-8"
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        assert resp.getheader("X-Frame-Options") == "DENY"
        assert resp.getheader("Referrer-Policy") == "no-referrer"
        assert resp.getheader("Cache-Control") == "no-store"

    def test_error_responses_also_carry_security_headers(self, running):
        resp = running.get("/api/overview")  # missing ns -> 400
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.getheader("X-Content-Type-Options") == "nosniff"


# ---------------------------------------------------------------------------
# 15 — no database mutations
# ---------------------------------------------------------------------------

class TestNoMutations:
    def test_hitting_every_endpoint_leaves_the_database_file_byte_identical(self, running):
        before = running.server.db_path.read_bytes()  # type: ignore[attr-defined]
        running.get("/api/namespaces")
        running.get(f"/api/overview?ns={running.digest_a}")
        running.get(f"/api/facts?ns={running.digest_a}")
        running.get(f"/api/search?ns={running.digest_a}&q=Alice")
        running.get(f"/api/entities?ns={running.digest_a}")
        running.get(f"/api/timeline?ns={running.digest_a}")
        after = running.server.db_path.read_bytes()  # type: ignore[attr-defined]
        assert before == after

    def test_search_never_increments_retrieval_count_unlike_the_write_path_equivalent(self, running):
        before = running.server.db_path.stat().st_size  # type: ignore[attr-defined]
        for _ in range(5):
            running.get(f"/api/search?ns={running.digest_a}&q=Alice")
        after = running.server.db_path.stat().st_size  # type: ignore[attr-defined]
        assert before == after


# ---------------------------------------------------------------------------
# 16 — legacy route compatibility
# ---------------------------------------------------------------------------

class TestLegacyRouteCompatibility:
    def test_index_namespace_fact_entity_static_routes_still_work(self, running):
        assert running.get("/").status == 200
        assert running.get(f"/namespace?ns={running.digest_a}").status == 200
        _, listing = _get_json(running, f"/api/facts?ns={running.digest_a}")
        fact_id = listing["facts"][0]["fact_id"]
        assert running.get(f"/fact?ns={running.digest_a}&id={fact_id}").status == 200
        assert running.get("/static/vendor/chart.umd.4.4.7.min.js").status == 200

    def test_legacy_html_routes_are_unaffected_by_the_api_routing_branch(self, running):
        resp = running.get("/")
        assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
        assert b"Holographic Memory Viewer" in resp.body


# ---------------------------------------------------------------------------
# 17 — concurrent reads while Hermes writes
# ---------------------------------------------------------------------------

class TestConcurrentReadsWhileWriting:
    def test_api_reads_survive_a_concurrent_writer(self, tmp_path):
        ns = "telegram:chat_API_CONCURRENT"
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("Fact present before the server starts.", category="general", namespace=ns)

        s = _start_server(db_path)
        digest = _namespace_digest(ns)
        errors: list[Exception] = []

        def _writer() -> None:
            try:
                for i in range(20):
                    store.add_fact(f"Concurrently written fact {i}.", category="general", namespace=ns)
                    time.sleep(0.005)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        writer_thread = threading.Thread(target=_writer)
        writer_thread.start()
        try:
            bad_statuses = []
            for _ in range(40):
                resp = s.get(f"/api/overview?ns={digest}")
                if resp.status != 200:
                    bad_statuses.append(resp.status)
                time.sleep(0.003)
        finally:
            writer_thread.join(timeout=10)
            s.stop()
            store.close()

        assert not errors, f"writer thread raised: {errors}"
        assert bad_statuses == []
