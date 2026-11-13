"""Phase 7 — optional local diagnostic viewer tests.

See docs/plans/demo-reference-plan/checklist.md section 11 ("Phase 7 —
Optional local viewer" and its "Viewer tests" subsection) and
implementation-plan.md's "Phase 7"/"9.10 Viewer tests, if Phase 7 is
implemented".

Uses the repo-wide `_isolate_hermes_home` autouse fixture (tests/conftest.py)
so HERMES_HOME is always a temp directory, never the real ~/.hermes/.
HTTP-level tests use stdlib `urllib.request`/`http.client` against a server
started on an ephemeral port (`port=0`) in-process, per the task's test
guidance (no new test-only HTTP client dependency).
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

class _RunningServer:
    def __init__(self, server, thread):
        self.server = server
        self.thread = thread

    @property
    def host(self):
        return self.server.server_address[0]

    @property
    def port(self):
        return self.server.server_address[1]

    def get(self, path: str) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.body = resp.read()
            return resp
        finally:
            conn.close()

    def request(self, method: str, path: str) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            resp.body = resp.read()
            return resp
        finally:
            conn.close()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _start_server(db_path, *, show_raw_identity: bool = False) -> _RunningServer:
    server = _viewer.create_server(db_path, host="127.0.0.1", port=0, show_raw_identity=show_raw_identity)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _RunningServer(server, thread)


@pytest.fixture
def seeded_db(tmp_path):
    """Two real namespaces, each with active/superseded facts, an entity
    relationship, and a tombstone — enough surface for every route."""
    db_path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)

    ns_a = "telegram:chat_AAA"
    ns_b = "telegram:chat_BBB"

    fid_a1 = store.add_fact("Alice prefers dark mode.", category="user_pref", namespace=ns_a)
    store.add_fact("Secret project codename is Nightjar.", category="project", namespace=ns_a)
    store.add_fact(
        "The API key is: sk-ant-abcdefghijklmnopqrstuvwxyz0123456789",
        category="tool", namespace=ns_a,
    )
    store.supersede_fact(fid_a1, "Alice now prefers light mode.", namespace=ns_a)

    store.add_fact("Bob works at Acme Corp.", category="project", namespace=ns_b)

    temp_id = store.add_fact("Temporary note to forget.", category="general", namespace=ns_a)
    store.remove_fact(temp_id, namespace=ns_a)

    store.close()
    return db_path, ns_a, ns_b


class TestLoopbackDefault:
    def test_create_server_binds_to_loopback_by_default(self, seeded_db):
        db_path, _, _ = seeded_db
        server = _viewer.create_server(db_path, port=0)
        try:
            assert server.server_address[0] in _viewer.LOOPBACK_HOSTS
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()

    def test_explicit_loopback_variants_accepted(self, seeded_db):
        db_path, _, _ = seeded_db
        for host in ("127.0.0.1", "localhost"):
            server = _viewer.create_server(db_path, host=host, port=0)
            server.server_close()

    def test_non_loopback_binding_is_refused(self, seeded_db):
        db_path, _, _ = seeded_db
        with pytest.raises(_viewer.NonLoopbackBindingNotSupported):
            _viewer.create_server(db_path, host="0.0.0.0", port=0)

    def test_non_loopback_refusal_happens_before_any_socket_is_opened(self, seeded_db):
        """A failed non-loopback attempt must not leave a listening socket
        bound anywhere — assert by trying a second, valid bind right after
        and confirming no ResourceWarning-style leak/port conflict occurs."""
        db_path, _, _ = seeded_db
        with pytest.raises(_viewer.NonLoopbackBindingNotSupported):
            _viewer.create_server(db_path, host="10.0.0.5", port=0)
        server = _viewer.create_server(db_path, host="127.0.0.1", port=0)
        server.server_close()


class TestMutationMethodsRejected:
    @pytest.fixture
    def running(self, seeded_db):
        db_path, _, _ = seeded_db
        s = _start_server(db_path)
        yield s
        s.stop()

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    @pytest.mark.parametrize("path", ["/", "/namespace", "/fact", "/entity"])
    def test_every_route_rejects_every_mutation_method(self, running, method, path):
        resp = running.request(method, path)
        assert resp.status in (404, 405)
        if resp.status == 405:
            assert resp.getheader("Allow") == "GET"

    def test_enumerated_routes_accept_only_get(self, running):
        """Enumerate every registered route this module serves and confirm
        none of them accept a mutation method."""
        routes = ["/", "/namespace", "/fact", "/entity", "/does-not-exist"]
        for path in routes:
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                resp = running.request(method, path)
                assert resp.status in (404, 405), f"{method} {path} returned {resp.status}"


class TestNamespaceSelectionExplicit:
    @pytest.fixture
    def running(self, seeded_db):
        db_path, ns_a, ns_b = seeded_db
        s = _start_server(db_path)
        s.ns_a = ns_a
        s.ns_b = ns_b
        yield s
        s.stop()

    def test_index_never_renders_facts(self, running):
        resp = running.get("/")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "dark mode" not in body
        assert "light mode" not in body
        assert "Acme" not in body
        # it's a selector: links to /namespace exist
        assert "/namespace?ns=" in body

    def test_namespace_route_without_ns_param_is_rejected(self, running):
        resp = running.get("/namespace")
        assert resp.status == 400

    def test_namespace_route_with_valid_digest_returns_only_that_namespace(self, running):
        digest_a = _namespace_digest(running.ns_a)
        resp = running.get(f"/namespace?ns={digest_a}")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "light mode" in body  # ns_a's surviving active fact
        assert "Acme" not in body  # ns_b's fact must never appear here

    def test_fact_route_without_ns_or_id_is_rejected(self, running):
        resp = running.get("/fact")
        assert resp.status == 400
        resp = running.get("/fact?ns=abc")
        assert resp.status == 400


class TestCrossNamespaceAccessRejected:
    @pytest.fixture
    def running(self, seeded_db):
        db_path, ns_a, ns_b = seeded_db
        s = _start_server(db_path)
        s.ns_a = ns_a
        s.ns_b = ns_b
        yield s
        s.stop()

    def test_unknown_digest_404s(self, running):
        resp = running.get("/namespace?ns=0000000000000000")
        assert resp.status == 404

    def test_fact_id_from_namespace_b_is_not_visible_under_namespace_a_digest(self, running):
        digest_a = _namespace_digest(running.ns_a)
        digest_b = _namespace_digest(running.ns_b)
        # Find ns_b's fact id by asking through its own (legitimate) digest.
        resp_b = running.get(f"/namespace?ns={digest_b}")
        body_b = resp_b.body.decode("utf-8")
        assert "Acme" in body_b
        import re
        m = re.search(r"/fact\?ns=" + re.escape(digest_b) + r"&id=(\d+)", body_b)
        assert m, "expected at least one fact link in namespace B's page"
        fact_id = m.group(1)

        # Now request that SAME fact_id, but under namespace A's digest —
        # this must not leak namespace B's content.
        resp_crafted = running.get(f"/fact?ns={digest_a}&id={fact_id}")
        assert resp_crafted.status == 404

        # And confirm it legitimately exists under its own namespace.
        resp_legit = running.get(f"/fact?ns={digest_b}&id={fact_id}")
        assert resp_legit.status == 200
        assert "Acme" in resp_legit.body.decode("utf-8")

    def test_crafted_raw_namespace_string_as_query_param_does_not_bypass_digest(self, running):
        """Even if a crafted client tries to pass something namespace-ish
        directly as `ns`, it is only ever treated as a digest to match
        against real data — never as a raw namespace selector."""
        resp = running.get(f"/namespace?ns={running.ns_a}")
        assert resp.status == 404


class TestPaginationAndQueryLimits:
    @pytest.fixture
    def running(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns = "telegram:chat_PAGE"
        for i in range(_viewer.MAX_PAGE_SIZE + 25):
            store.add_fact(f"Paginated fact number {i}.", category="general", namespace=ns)
        store.close()
        s = _start_server(db_path)
        s.ns = ns
        yield s
        s.stop()

    def test_default_page_is_capped(self, running):
        digest = _namespace_digest(running.ns)
        resp = running.get(f"/namespace?ns={digest}")
        body = resp.body.decode("utf-8")
        assert body.count("/fact?ns=") <= _viewer.MAX_PAGE_SIZE

    def test_requested_per_page_above_cap_is_clamped(self, running):
        digest = _namespace_digest(running.ns)
        resp = running.get(f"/namespace?ns={digest}&per_page=99999")
        body = resp.body.decode("utf-8")
        assert body.count("/fact?ns=") <= _viewer.MAX_PAGE_SIZE

    def test_page_beyond_the_last_returns_no_facts_not_an_error(self, running):
        digest = _namespace_digest(running.ns)
        resp = running.get(f"/namespace?ns={digest}&page=9999")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert body.count("/fact?ns=") == 0

    def test_index_namespace_listing_is_also_paginated(self, tmp_path):
        db_path = tmp_path / "many_ns.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        for i in range(_viewer.MAX_PAGE_SIZE + 10):
            store.add_fact(f"fact {i}", category="general", namespace=f"telegram:chat_{i}")
        store.close()
        s = _start_server(db_path)
        try:
            resp = s.get("/")
            body = resp.body.decode("utf-8")
            assert body.count("/namespace?ns=") <= _viewer.MAX_PAGE_SIZE
        finally:
            s.stop()


class TestSqlInjectionShapedSearchIsLiteral:
    @pytest.fixture
    def running(self, seeded_db):
        db_path, ns_a, ns_b = seeded_db
        s = _start_server(db_path)
        s.ns_a = ns_a
        yield s
        s.stop()

    def test_injection_shaped_query_is_treated_as_literal_search_term(self, running):
        digest_a = _namespace_digest(running.ns_a)
        payload = "' OR 1=1 --"
        resp = running.get(f"/namespace?ns={digest_a}&q={http_quote(payload)}")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        # A literal search for this nonsense string matches nothing, and
        # critically does not error out or dump every row in the namespace.
        assert body.count("/fact?ns=") == 0

    def test_injection_shaped_query_does_not_raise_or_corrupt_response(self, running):
        digest_a = _namespace_digest(running.ns_a)
        for payload in ["'; DROP TABLE facts; --", "\" OR \"\"=\"", "%' UNION SELECT * FROM facts --"]:
            resp = running.get(f"/namespace?ns={digest_a}&q={http_quote(payload)}")
            assert resp.status == 200


def http_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


class TestSecretsAndIdentityRedaction:
    @pytest.fixture
    def running(self, seeded_db):
        db_path, ns_a, ns_b = seeded_db
        s = _start_server(db_path, show_raw_identity=False)
        s.ns_a = ns_a
        s.ns_b = ns_b
        yield s
        s.stop()

    def test_secret_shaped_content_is_redacted_everywhere(self, running):
        digest_a = _namespace_digest(running.ns_a)
        resp = running.get(f"/namespace?ns={digest_a}")
        body = resp.body.decode("utf-8")
        assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in body
        assert "REDACTED" in body

        import re
        m = re.search(r"/fact\?ns=" + re.escape(digest_a) + r"&id=(\d+)", body)
        # find the specific fact with the secret by scanning all fact pages
        found_redacted_detail = False
        for fid_match in re.finditer(r"/fact\?ns=" + re.escape(digest_a) + r"&id=(\d+)", body):
            detail = running.get(f"/fact?ns={digest_a}&id={fid_match.group(1)}")
            detail_body = detail.body.decode("utf-8")
            assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in detail_body
            if "REDACTED" in detail_body:
                found_redacted_detail = True
        assert found_redacted_detail

    def test_raw_namespace_string_never_appears_by_default(self, running):
        resp = running.get("/")
        body = resp.body.decode("utf-8")
        assert running.ns_a not in body
        assert running.ns_b not in body

    def test_show_raw_identity_opt_in_reveals_namespace_only_when_explicitly_enabled(self, seeded_db):
        db_path, ns_a, ns_b = seeded_db
        s = _start_server(db_path, show_raw_identity=True)
        try:
            resp = s.get("/")
            body = resp.body.decode("utf-8")
            assert ns_a in body or ns_b in body
        finally:
            s.stop()

    def test_forgotten_fact_content_is_never_retrievable(self, running):
        """Forgotten facts are hard-deleted (see store.remove_fact) — there
        is no content left to leak, but confirm the namespace page shows a
        distinct tombstone count rather than fabricating a fact entry, and
        that the forgotten text itself never appears anywhere."""
        digest_a = _namespace_digest(running.ns_a)
        resp = running.get(f"/namespace?ns={digest_a}")
        body = resp.body.decode("utf-8")
        assert "Temporary note to forget" not in body
        assert "forgotten" in body.lower()


class TestStatusDistinctness:
    @pytest.fixture
    def running(self, seeded_db):
        db_path, ns_a, ns_b = seeded_db
        s = _start_server(db_path)
        s.ns_a = ns_a
        yield s
        s.stop()

    def test_active_and_superseded_use_different_badge_classes(self, running):
        digest_a = _namespace_digest(running.ns_a)
        resp = running.get(f"/namespace?ns={digest_a}")
        body = resp.body.decode("utf-8")
        assert "status-active" in body
        assert "status-superseded" in body


class TestRevisionAndRelationshipProvenance:
    def test_fact_page_links_supersession_ancestor(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns = "telegram:chat_REV"
        old_id = store.add_fact("Old value.", category="general", namespace=ns)
        store.supersede_fact(old_id, "New value.", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp = s.get(f"/namespace?ns={digest}")
            body = resp.body.decode("utf-8")
            import re
            new_id_match = re.search(r"/fact\?ns=" + re.escape(digest) + r"&id=(\d+)", body)
            assert new_id_match
            new_id = new_id_match.group(1)
            detail = s.get(f"/fact?ns={digest}&id={new_id}")
            assert "Revision chain" in detail.body.decode("utf-8")
        finally:
            s.stop()

    def test_entity_page_lists_relationships(self, tmp_path):
        from plugins.memory.holographic import relationships as relationships_module

        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns = "telegram:chat_ENT"
        fid = store.add_fact('"Alice" works with "Bob".', category="project", namespace=ns)
        rows = store._conn.execute("SELECT entity_id, name FROM entities").fetchall()
        assert len(rows) >= 2
        a_id, b_id = rows[0]["entity_id"], rows[1]["entity_id"]
        store.upsert_relationship(a_id, b_id, "co_occurs_with", namespace=ns)
        store.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp = s.get(f"/entity?ns={digest}&id={a_id}")
            assert resp.status == 200
            body = resp.body.decode("utf-8")
            assert "Relationships" in body
            assert "co_occurs_with" in body
            assert "Bounded traversal" in body
        finally:
            s.stop()

    def test_entity_from_other_namespace_404s(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns_a = "telegram:chat_X"
        ns_b = "telegram:chat_Y"
        store.add_fact('"Carol" likes tea.', category="general", namespace=ns_b)
        row = store._conn.execute("SELECT entity_id FROM entities WHERE name = 'Carol'").fetchone()
        assert row is not None
        entity_id = row["entity_id"]
        store.add_fact("Unrelated fact.", category="general", namespace=ns_a)
        store.close()

        s = _start_server(db_path)
        try:
            digest_a = _namespace_digest(ns_a)
            resp = s.get(f"/entity?ns={digest_a}&id={entity_id}")
            assert resp.status == 404
        finally:
            s.stop()


class TestReadOnlyGuarantee:
    def test_viewer_never_mutates_the_database(self, seeded_db):
        import hashlib

        db_path, ns_a, ns_b = seeded_db
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        s = _start_server(db_path)
        try:
            digest_a = _namespace_digest(ns_a)
            s.get("/")
            s.get(f"/namespace?ns={digest_a}")
            resp = s.get(f"/namespace?ns={digest_a}")
            body = resp.body.decode("utf-8")
            import re
            for m in re.finditer(r"/fact\?ns=" + re.escape(digest_a) + r"&id=(\d+)", body):
                s.get(f"/fact?ns={digest_a}&id={m.group(1)}")
        finally:
            s.stop()
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after

    def test_open_conn_is_truly_read_only(self, seeded_db):
        db_path, _, _ = seeded_db
        conn = _viewer._open_conn(db_path)
        try:
            with pytest.raises(Exception):
                conn.execute("INSERT INTO facts (content, namespace) VALUES ('x', '')")
        finally:
            conn.close()
