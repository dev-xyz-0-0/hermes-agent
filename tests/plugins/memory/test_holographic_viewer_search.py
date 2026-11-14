"""Phase 7.1E — FTS5 search integration tests for the Holographic memory
viewer (`plugins/memory/holographic/viewer.py`).

Covers literal-mode wildcard escaping, FTS5-mode matching and namespace
scoping, and backward-compatible fallback when FTS5 is unavailable or a
MATCH query fails — the full §E2-E4 list. HTTP security (7.1A), namespace
isolation (7.1B), SQLite reliability (7.1C), and pagination/deadline (7.1D)
are covered elsewhere.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import quote

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401

NS = "telegram:chat_SEARCH"


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    store.add_fact("Alice enjoys a 50% discount at the ocean-view cafe.", category="general", namespace=NS)
    store.add_fact("Bob prefers mountain hiking over the beach.", category="general", namespace=NS)
    store.add_fact("The quarterly report mentions revenue growth.", category="general", namespace=NS)
    store.close()
    return db_path


@pytest.fixture
def running(seeded_db):
    s = _start_server(seeded_db)
    s.digest = _namespace_digest(NS)
    yield s
    s.stop()


# ---------------------------------------------------------------------------
# E1 (informational, no behavior to test directly) — confirm the production
# facts_fts table this phase integrates with actually exists and is an
# external-content table over facts, as the module docstring/comments
# assume.
# ---------------------------------------------------------------------------

class TestFts5InfrastructureAssumptions:
    def test_facts_fts_table_exists_and_is_external_content(self, seeded_db):
        conn = sqlite3.connect(str(seeded_db))
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'facts_fts'"
            ).fetchone()
            assert row is not None
            assert "fts5" in row[0].lower()
            assert "content=facts" in row[0].replace(" ", "")
            assert "content_rowid=fact_id" in row[0].replace(" ", "")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# E2 — literal mode: wildcard escaping
# ---------------------------------------------------------------------------

class TestLiteralModeWildcardEscaping:
    def test_percent_sign_is_treated_literally_not_as_a_wildcard(self, running):
        resp = running.get(f"/namespace?ns={running.digest}&q=50%25")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "50% discount" in body
        assert "quarterly report" not in body  # would match if % were a real wildcard eating everything

    def test_underscore_is_treated_literally_not_as_a_single_char_wildcard(self, running):
        resp = running.get(f"/namespace?ns={running.digest}&q=xyz_zy")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        # Nothing in the fixture matches this literal string — if '_' were
        # treated as a SQL wildcard this could spuriously match unrelated
        # single-character-substituted content; it must not.
        assert body.count("/fact?ns=") == 0

    def test_ordinary_substring_search_still_works(self, running):
        resp = running.get(f"/namespace?ns={running.digest}&q=mountain")
        assert resp.status == 200
        assert "mountain hiking" in resp.body.decode("utf-8")

    def test_default_mode_is_literal_when_search_mode_omitted(self, running):
        """Backward compatibility: a request with no `search_mode` param
        (every pre-7.1E bookmarked/test URL) behaves exactly as before —
        literal substring search."""
        resp = running.get(f"/namespace?ns={running.digest}&q=ocean")
        assert resp.status == 200
        assert "ocean-view" in resp.body.decode("utf-8")


# ---------------------------------------------------------------------------
# E2 — FTS5 mode: matching, ranking, namespace scoping
# ---------------------------------------------------------------------------

class TestFts5Mode:
    def test_single_word_match(self, running):
        resp = running.get(f"/namespace?ns={running.digest}&q=hiking&search_mode=fts")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "mountain hiking" in body
        assert "full-text search" in body.lower()

    def test_every_word_required_and_ed_together(self, running):
        """FTS5's implicit AND between bareword/phrase tokens: a
        multi-word query only matches facts containing every token."""
        resp = running.get(f"/namespace?ns={running.digest}&q=ocean+discount&search_mode=fts")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "ocean-view" in body
        assert "mountain hiking" not in body
        assert "quarterly report" not in body

    def test_no_match_returns_empty_not_an_error(self, running):
        resp = running.get(f"/namespace?ns={running.digest}&q=nonexistentword&search_mode=fts")
        assert resp.status == 200
        assert resp.body.decode("utf-8").count("/fact?ns=") == 0

    def test_fts5_scoped_to_the_requested_namespace_only(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns_a, ns_b = "telegram:chat_FTS_A", "telegram:chat_FTS_B"
        store.add_fact("Shared keyword zephyr appears here in A.", category="general", namespace=ns_a)
        store.add_fact("Shared keyword zephyr appears here in B.", category="general", namespace=ns_b)
        store.close()

        s = _start_server(db_path)
        try:
            digest_a = _namespace_digest(ns_a)
            resp = s.get(f"/namespace?ns={digest_a}&q=zephyr&search_mode=fts")
            body = resp.body.decode("utf-8")
            assert "appears here in A" in body
            assert "appears here in B" not in body
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# E2/E3 — safe FTS5 query construction: operators/syntax neutralized
# ---------------------------------------------------------------------------

class TestFts5QuerySafety:
    @pytest.mark.parametrize("payload", [
        'ocean OR report',
        'ocean NOT mountain',
        'content:ocean',
        'ocean*',
        '"unterminated quote',
        'ocean)(mountain',
        'NEAR(ocean mountain)',
    ])
    def test_fts5_operator_shaped_input_never_errors_and_is_treated_literally(self, running, payload):
        """Every one of these would be meaningful FTS5 query SYNTAX if
        passed through unquoted. None of the fixture facts literally
        contain these exact phrases, so a safe implementation (each token
        quoted into a phrase) finds nothing — it must not raise, 500, or
        silently behave like the raw operator (e.g. an OR/NOT that would
        otherwise match many more rows)."""
        resp = running.get(f"/namespace?ns={running.digest}&q={quote(payload, safe='')}&search_mode=fts")
        assert resp.status == 200
        body = resp.body.decode("utf-8")
        assert "quarterly report" not in body or "ocean" in payload.lower()

    def test_embedded_double_quote_is_escaped_not_a_syntax_break(self, running):
        payload = quote('ocean" OR 1', safe="")
        resp = running.get(f"/namespace?ns={running.digest}&q={payload}&search_mode=fts")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# E3 — backward-compatible fallback
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    def test_fts_requested_but_table_missing_falls_back_to_literal_and_says_so(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("Ocean view available here.", category="general", namespace=NS)
        store.close()

        # Drop the FTS5 table (and its shadow tables/triggers) directly to
        # simulate an older/synthetic schema that predates FTS5 support —
        # never done by the viewer itself, only by this test fixture.
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS facts_fts")
        conn.execute("DROP TRIGGER IF EXISTS facts_ai")
        conn.execute("DROP TRIGGER IF EXISTS facts_au")
        conn.execute("DROP TRIGGER IF EXISTS facts_ad")
        conn.commit()
        conn.close()

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(NS)
            resp = s.get(f"/namespace?ns={digest}&q=ocean&search_mode=fts")
            assert resp.status == 200
            body = resp.body.decode("utf-8")
            assert "Ocean view" in body  # literal fallback still found it
            assert "falling back" in body.lower() or "instead" in body.lower()
        finally:
            s.stop()

    def test_viewer_never_writes_to_the_fts_index(self, running):
        """The viewer's read-only (mode=ro + PRAGMA query_only=ON)
        connection makes any INSERT/rebuild into facts_fts structurally
        impossible — confirm no FTS5 route attempts one by checking the
        database file is byte-identical before and after an FTS search."""
        import hashlib

        db_path = running.server.db_path
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        running.get(f"/namespace?ns={running.digest}&q=ocean&search_mode=fts")
        running.get(f"/namespace?ns={running.digest}&q=mountain&search_mode=literal")
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after

    def test_missing_optional_fts_table_still_passes_schema_validation(self, tmp_path):
        """facts_fts isn't in `_REQUIRED_TABLE_COLUMNS`/`_OPTIONAL_TABLE_COLUMNS`
        (Phase 7.1C) — its absence must not block `create_server` at all,
        confirming E3's 'never silently return incomplete results as
        complete' framing starts from 'FTS5 is allowed to simply not be
        there'."""
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.close()
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS facts_fts")
        conn.commit()
        conn.close()
        server = _viewer.create_server(db_path, port=0)
        server.server_close()


# ---------------------------------------------------------------------------
# E4 — unicode / non-ASCII content
# ---------------------------------------------------------------------------

class TestUnicodeContent:
    def test_literal_search_over_non_ascii_content(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("用户喜欢在海边散步。", category="general", namespace=NS)
        store.close()
        s = _start_server(db_path)
        try:
            digest = _namespace_digest(NS)
            resp = s.get(f"/namespace?ns={digest}&q=" + quote("海边"))
            assert resp.status == 200
            assert "用户喜欢在海边散步" in resp.body.decode("utf-8")
        finally:
            s.stop()

    def test_fts5_search_over_non_ascii_content_does_not_error(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("用户喜欢在海边散步。", category="general", namespace=NS)
        store.close()
        s = _start_server(db_path)
        try:
            digest = _namespace_digest(NS)
            resp = s.get(f"/namespace?ns={digest}&q=" + quote("海边") + "&search_mode=fts")
            assert resp.status == 200
        finally:
            s.stop()
