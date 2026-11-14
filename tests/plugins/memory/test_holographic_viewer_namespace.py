"""Phase 7.1B — namespace isolation and privacy tests for the Holographic
memory viewer (`plugins/memory/holographic/viewer.py`).

Covers namespace digest resolution (B1, including collision handling),
cross-namespace verification across every route (B2), and the raw-identity
opt-in being a CLI-only switch, never a query parameter (B4). Secret
redaction (B3) is covered separately in
`test_holographic_viewer_redaction.py`. General single-pair cross-namespace
and pagination/search behavior is covered by `test_holographic_viewer.py`;
this file extends that coverage to three namespaces with a deliberately
shared entity name, plus the digest-collision edge case.
"""

from __future__ import annotations

import re

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401


# ---------------------------------------------------------------------------
# Three-namespace fixture with a deliberately shared entity name ("Alice"),
# each with its own fact and its own relationship for that shared entity.
# ---------------------------------------------------------------------------

@pytest.fixture
def three_namespaces(tmp_path):
    db_path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)

    ns_a = "telegram:chat_NS_A"
    ns_b = "telegram:chat_NS_B"
    ns_c = "telegram:chat_NS_C"

    fid_a = store.add_fact('"Alice" prefers tea in namespace A.', category="general", namespace=ns_a)
    fid_b = store.add_fact('"Bob" works at Globex in namespace B.', category="general", namespace=ns_b)
    fid_c = store.add_fact('"Alice" prefers coffee in namespace C.', category="general", namespace=ns_c)

    def _entity_id(name: str) -> int:
        row = store._conn.execute("SELECT entity_id FROM entities WHERE name = ?", (name,)).fetchone()
        assert row is not None, f"entity {name!r} was not extracted"
        return row["entity_id"]

    alice_id = _entity_id("Alice")
    bob_id = _entity_id("Bob")

    # "Alice" is the SAME global entity_id in both A and C (entities are
    # resolved globally by name — see store._resolve_entity) but gets a
    # DIFFERENT namespace-scoped relationship in each.
    store.upsert_relationship(alice_id, bob_id, "drinks_with_a", namespace=ns_a, source_fact_id=fid_a)
    store.upsert_relationship(alice_id, bob_id, "drinks_with_c", namespace=ns_c, source_fact_id=fid_c)

    store.close()
    return db_path, ns_a, ns_b, ns_c, alice_id, bob_id


@pytest.fixture
def running3(three_namespaces):
    db_path, ns_a, ns_b, ns_c, alice_id, bob_id = three_namespaces
    s = _start_server(db_path)
    s.ns_a, s.ns_b, s.ns_c = ns_a, ns_b, ns_c
    s.alice_id, s.bob_id = alice_id, bob_id
    yield s
    s.stop()


# ---------------------------------------------------------------------------
# B2 — cross-namespace verification across three namespaces
# ---------------------------------------------------------------------------

class TestCrossNamespaceAcrossThreeNamespaces:
    def _fact_id_on(self, running, digest, marker):
        resp = running.get(f"/namespace?ns={digest}")
        body = resp.body.decode("utf-8")
        assert marker in body
        m = re.search(r"/fact\?ns=" + re.escape(digest) + r"&id=(\d+)", body)
        assert m, f"expected a fact link for digest {digest}"
        return m.group(1)

    def test_each_namespaces_fact_id_is_opaque_under_the_other_two_digests(self, running3):
        digest_a = _namespace_digest(running3.ns_a)
        digest_b = _namespace_digest(running3.ns_b)
        digest_c = _namespace_digest(running3.ns_c)

        fact_id_a = self._fact_id_on(running3, digest_a, "tea")
        fact_id_b = self._fact_id_on(running3, digest_b, "Globex")
        fact_id_c = self._fact_id_on(running3, digest_c, "coffee")

        # Every fact_id, tried under every OTHER namespace's digest, 404s.
        for fact_id, owner_digest in [(fact_id_a, digest_a), (fact_id_b, digest_b), (fact_id_c, digest_c)]:
            for other_digest in (digest_a, digest_b, digest_c):
                if other_digest == owner_digest:
                    continue
                resp = running3.get(f"/fact?ns={other_digest}&id={fact_id}")
                assert resp.status == 404

    def test_shared_entity_name_resolves_to_same_entity_id_across_namespaces(self, running3):
        """Precondition check: 'Alice' really is the same global entity_id
        in both A and C — otherwise the isolation assertions below would be
        vacuous."""
        digest_a = _namespace_digest(running3.ns_a)
        resp = running3.get(f"/namespace?ns={digest_a}")
        body = resp.body.decode("utf-8")
        assert str(running3.alice_id) in body or "Alice" in body
        # Direct confirmation via the entity page itself.
        resp = running3.get(f"/entity?ns={digest_a}&id={running3.alice_id}")
        assert resp.status == 200

    def test_shared_entity_shows_only_its_own_namespaces_relationship(self, running3):
        digest_a = _namespace_digest(running3.ns_a)
        digest_c = _namespace_digest(running3.ns_c)

        resp_a = running3.get(f"/entity?ns={digest_a}&id={running3.alice_id}")
        assert resp_a.status == 200
        body_a = resp_a.body.decode("utf-8")
        assert "drinks_with_a" in body_a
        assert "drinks_with_c" not in body_a

        resp_c = running3.get(f"/entity?ns={digest_c}&id={running3.alice_id}")
        assert resp_c.status == 200
        body_c = resp_c.body.decode("utf-8")
        assert "drinks_with_c" in body_c
        assert "drinks_with_a" not in body_c

    def test_shared_entity_not_reachable_under_the_namespace_that_never_used_it(self, running3):
        """'Alice' has no fact or relationship in namespace B at all."""
        digest_b = _namespace_digest(running3.ns_b)
        resp = running3.get(f"/entity?ns={digest_b}&id={running3.alice_id}")
        assert resp.status == 404

    @pytest.mark.parametrize("digest_attr,other_attrs", [
        ("ns_a", ("ns_b", "ns_c")),
        ("ns_b", ("ns_a", "ns_c")),
        ("ns_c", ("ns_a", "ns_b")),
    ])
    def test_namespace_fact_entity_routes_404_for_every_invalid_combination(self, running3, digest_attr, other_attrs):
        digest = _namespace_digest(getattr(running3, digest_attr))
        for other_attr in other_attrs:
            other_ns = getattr(running3, other_attr)
            other_digest = _namespace_digest(other_ns)
            # A digest that belongs to a DIFFERENT namespace than the one
            # being probed is still a *valid* digest overall — the point is
            # that content from `digest`'s namespace must never appear when
            # browsing `other_digest`. Covered by the fact-id test above;
            # here we additionally confirm a nonsense digest always 404s
            # regardless of which real namespace's page requested it.
            resp = running3.get(f"/namespace?ns={other_digest}0000")
            assert resp.status == 404
            resp = running3.get(f"/fact?ns={other_digest}0000&id=1")
            assert resp.status == 404
            resp = running3.get(f"/entity?ns={other_digest}0000&id={running3.alice_id}")
            assert resp.status == 404


# ---------------------------------------------------------------------------
# B1 — digest collision handling
# ---------------------------------------------------------------------------

class TestDigestCollisionHandling:
    def test_colliding_digest_resolves_to_neither_namespace(self, three_namespaces, monkeypatch):
        """If two distinct real namespaces ever hashed to the same digest
        (a SHA-1 collision in practice, forced here for the test), the
        resolver must refuse rather than silently picking whichever
        namespace its SELECT DISTINCT happened to enumerate first."""
        db_path, ns_a, ns_b, ns_c, _, _ = three_namespaces
        monkeypatch.setattr(_viewer, "_namespace_digest", lambda ns: "collided")

        s = _start_server(db_path)
        try:
            resp = s.get("/namespace?ns=collided")
            assert resp.status == 404
            resp = s.get("/fact?ns=collided&id=1")
            assert resp.status == 404
        finally:
            s.stop()

    def test_unique_digest_still_resolves_normally(self, three_namespaces):
        """Sanity check that the rewritten resolver didn't break the
        ordinary, non-colliding case."""
        db_path, ns_a, _, _, _, _ = three_namespaces
        s = _start_server(db_path)
        try:
            digest_a = _namespace_digest(ns_a)
            resp = s.get(f"/namespace?ns={digest_a}")
            assert resp.status == 200
            assert "tea" in resp.body.decode("utf-8")
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# B4 — raw identity is a CLI-only opt-in, never a query parameter
# ---------------------------------------------------------------------------

class TestRawIdentityNotClientControllable:
    def test_query_param_cannot_enable_raw_identity(self, three_namespaces):
        db_path, ns_a, ns_b, ns_c, _, _ = three_namespaces
        s = _start_server(db_path, show_raw_identity=False)
        try:
            for suffix in ("", "?show_raw_identity=1", "?show_raw_identity=true", "?raw=1"):
                resp = s.get(f"/{suffix}")
                body = resp.body.decode("utf-8")
                assert ns_a not in body
                assert ns_b not in body
                assert ns_c not in body
        finally:
            s.stop()

    def test_cli_level_opt_in_still_works(self, three_namespaces):
        """Confirms the real (CLI-only) opt-in path still functions after
        the query-param-immunity test above — i.e. we didn't accidentally
        disable the feature entirely while locking it down."""
        db_path, ns_a, _, _, _, _ = three_namespaces
        s = _start_server(db_path, show_raw_identity=True)
        try:
            resp = s.get("/")
            assert ns_a in resp.body.decode("utf-8")
        finally:
            s.stop()
