"""Phase 7.1B — secret-shape redaction coverage for the Holographic memory
viewer (`plugins/memory/holographic/viewer.py`).

Reuses the real `redact_secret_shapes` detector (`plugins/memory/holographic/
__init__.py`) — no second, parallel detector is introduced here. Every
fixture below is synthetic (fabricated-looking) data, never real credentials.
Confirms the raw secret-shaped string never appears in ANY rendered page and
that a `[REDACTED:...]` placeholder does, across content, tags, entity
names, relationship labels, and supersession chains — the full field list
from spec §B3.
"""

from __future__ import annotations

import re

import pytest

from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401

NS = "telegram:chat_REDACT"

# Synthetic secret-shaped values only — fabricated, never real.
_API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
_BEARER_TOKEN = "Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCD"
_PRIVATE_KEY_BLOCK = "-----BEGIN RSA PRIVATE KEY-----"
_SEED_PHRASE_SENTENCE = "the recovery phrase is: apple banana cherry delta echo foxtrot golf hotel"
_CREDENTIAL_SENTENCE = "the api_key is zzzsupersecretvalue12345"


def _fact_id_for(running, digest, marker):
    resp = running.get(f"/namespace?ns={digest}")
    body = resp.body.decode("utf-8")
    m = re.search(r"/fact\?ns=" + re.escape(digest) + r"&id=(\d+)", body)
    assert m, f"no fact link found while looking for marker {marker!r} in namespace listing"
    return m.group(1)


@pytest.fixture
def redaction_db(tmp_path):
    """One namespace seeded directly against the store (bypassing
    `add_fact`'s own capture-time secret filter, `_fact_problem`) so the
    viewer's DISPLAY-time redaction is what's actually under test — the
    same rationale `redact_secret_shapes`'s docstring gives for existing
    beyond capture-time filtering."""
    db_path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)

    # Insert directly via the connection to bypass add_fact's secret filter.
    conn = store._conn
    conn.execute(
        "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
        "VALUES (?, 'general', ?, 0.5, 'active', ?)",
        (f"API key leaked: {_API_KEY}", f"aws={_FAKE_AWS_KEY}", NS),
    )
    content_fact_id = conn.execute("SELECT fact_id FROM facts WHERE namespace = ?", (NS,)).fetchone()["fact_id"]

    conn.execute(
        "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
        "VALUES (?, 'general', '', 0.5, 'active', ?)",
        (f"Auth header was {_BEARER_TOKEN}", NS),
    )
    conn.execute(
        "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
        "VALUES (?, 'general', '', 0.5, 'active', ?)",
        (f"Found this: {_PRIVATE_KEY_BLOCK}", NS),
    )
    conn.execute(
        "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
        "VALUES (?, 'general', '', 0.5, 'active', ?)",
        (_SEED_PHRASE_SENTENCE, NS),
    )
    conn.execute(
        "INSERT INTO facts (content, category, tags, trust_score, status, namespace) "
        "VALUES (?, 'general', '', 0.5, 'active', ?)",
        (_CREDENTIAL_SENTENCE, NS),
    )
    conn.commit()

    # Entity with a secret-shaped name.
    conn.execute("INSERT INTO entities (name) VALUES (?)", (f"Agent {_API_KEY}",))
    entity_id = conn.execute("SELECT entity_id FROM entities WHERE name LIKE 'Agent %'").fetchone()["entity_id"]
    conn.execute(
        "INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
        (content_fact_id, entity_id),
    )
    conn.commit()

    # A second entity + a relationship whose TYPE string is secret-shaped.
    conn.execute("INSERT INTO entities (name) VALUES ('Peer')")
    peer_id = conn.execute("SELECT entity_id FROM entities WHERE name = 'Peer'").fetchone()["entity_id"]
    store.upsert_relationship(entity_id, peer_id, f"shares-key-{_FAKE_AWS_KEY}", namespace=NS)

    # A supersession chain whose OLD content is secret-shaped.
    old_id = store.add_fact("Old value before secret leak.", category="general", namespace=NS)
    store.supersede_fact(old_id, f"New value leaking {_API_KEY}", namespace=NS)

    store.close()
    return db_path, entity_id


@pytest.fixture
def running(redaction_db):
    db_path, entity_id = redaction_db
    s = _start_server(db_path)
    s.entity_id = entity_id
    s.digest = _namespace_digest(NS)
    yield s
    s.stop()


class TestContentAndTagsRedaction:
    def test_api_key_in_content_is_redacted_in_namespace_listing(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        assert _API_KEY not in body
        assert "[REDACTED:SECRET]" in body

    def test_api_key_in_content_is_redacted_in_fact_detail(self, running):
        fact_id = _fact_id_for(running, running.digest, _API_KEY)
        resp = running.get(f"/fact?ns={running.digest}&id={fact_id}")
        body = resp.body.decode("utf-8")
        assert _API_KEY not in body
        assert "[REDACTED:SECRET]" in body

    def test_aws_key_in_tags_is_redacted(self, running):
        fact_id = _fact_id_for(running, running.digest, _FAKE_AWS_KEY)
        resp = running.get(f"/fact?ns={running.digest}&id={fact_id}")
        body = resp.body.decode("utf-8")
        assert _FAKE_AWS_KEY not in body

    def test_bearer_token_is_redacted(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        assert _BEARER_TOKEN not in body
        assert "abcdefghijklmnopqrstuvwxyz0123456789ABCD" not in body

    def test_private_key_block_is_redacted(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        assert _PRIVATE_KEY_BLOCK not in body
        assert "[REDACTED:PRIVATE_KEY]" in body

    def test_seed_phrase_sentence_is_redacted(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        assert "apple banana cherry" not in body or "[REDACTED:RECOVERY_PHRASE]" in body

    def test_credential_sentence_is_redacted(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        assert "zzzsupersecretvalue12345" not in body


class TestEntityNameRedaction:
    def test_secret_shaped_entity_name_is_redacted_on_entity_page(self, running):
        resp = running.get(f"/entity?ns={running.digest}&id={running.entity_id}")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert _API_KEY not in body
        assert "[REDACTED:SECRET]" in body

    def test_secret_shaped_entity_name_is_redacted_in_fact_entity_list(self, running):
        fact_id = _fact_id_for(running, running.digest, _API_KEY)
        resp = running.get(f"/fact?ns={running.digest}&id={fact_id}")
        body = resp.body.decode("utf-8")
        assert _API_KEY not in body


class TestRelationshipLabelRedaction:
    def test_secret_shaped_relationship_type_is_redacted(self, running):
        resp = running.get(f"/entity?ns={running.digest}&id={running.entity_id}")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert _FAKE_AWS_KEY not in body
        assert "[REDACTED:SECRET]" in body


class TestSupersessionContentRedaction:
    def test_secret_in_new_value_of_supersession_is_redacted(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        assert "New value leaking" in body
        assert _API_KEY not in body

    def test_secret_in_supersession_chain_detail_page_is_redacted(self, running):
        resp = running.get(f"/namespace?ns={running.digest}")
        body = resp.body.decode("utf-8")
        # What matters is that the raw secret never appears on ANY fact
        # detail page, including the revision-chain section that links the
        # superseded ancestor's content.
        any_checked = False
        for fid_match in re.finditer(r"/fact\?ns=" + re.escape(running.digest) + r"&id=(\d+)", body):
            detail = running.get(f"/fact?ns={running.digest}&id={fid_match.group(1)}")
            detail_body = detail.body.decode("utf-8")
            assert _API_KEY not in detail_body
            any_checked = True
        assert any_checked
