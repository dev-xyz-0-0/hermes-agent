"""Phase 7.1C — SQLite read-only reliability tests for the Holographic
memory viewer (`plugins/memory/holographic/viewer.py`).

Covers startup schema validation (C1/C4), the connection-lifecycle
guarantee (C2), and WAL-mode concurrency with a real writer (C3/C5). HTTP
security (7.1A) and namespace isolation/redaction (7.1B) are covered
elsewhere; pagination/search performance is Phase 7.1D/E, out of scope
here.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401


# ---------------------------------------------------------------------------
# C1/C4 — schema validation at `create_server` time
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_non_sqlite_file_raises_unsupported_schema_error(self, tmp_path):
        db_path = tmp_path / "not_a_db.db"
        db_path.write_bytes(b"this is definitely not a sqlite database")
        with pytest.raises(_viewer.UnsupportedSchemaError):
            _viewer.create_server(db_path, port=0)

    def test_missing_facts_table_raises_unsupported_schema_error(self, tmp_path):
        db_path = tmp_path / "empty_schema.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        with pytest.raises(_viewer.UnsupportedSchemaError, match="facts"):
            _viewer.create_server(db_path, port=0)

    def test_facts_table_missing_required_column_raises_unsupported_schema_error(self, tmp_path):
        db_path = tmp_path / "partial_schema.db"
        conn = sqlite3.connect(str(db_path))
        # A stripped-down facts table missing most of the real columns
        # (no 'namespace', no 'status', etc.) — and the other two required
        # tables, so only the 'facts' half of the schema is broken.
        conn.execute("CREATE TABLE facts (fact_id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("CREATE TABLE entities (entity_id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT)")
        conn.execute("CREATE TABLE fact_entities (fact_id INTEGER, entity_id INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(_viewer.UnsupportedSchemaError, match="facts"):
            _viewer.create_server(db_path, port=0)

    def test_healthy_database_still_constructs_successfully(self, tmp_path):
        """Regression: a normal MemoryStore-created database is unaffected
        by the new validation."""
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        server = _viewer.create_server(db_path, port=0)
        try:
            assert server.server_address[1] > 0
        finally:
            server.server_close()

    def test_missing_file_still_raises_file_not_found_before_schema_check(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _viewer.create_server(tmp_path / "does_not_exist.db", port=0)

    def test_optional_tables_missing_entirely_are_not_an_error(self, tmp_path):
        """entity_relationships/forgotten_facts are optional — a database
        that genuinely never created them must still pass validation (the
        existing `_table_exists` gating elsewhere already handles their
        absence gracefully)."""
        db_path = tmp_path / "no_optional_tables.db"
        conn = sqlite3.connect(str(db_path))
        for table, cols in _viewer._REQUIRED_TABLE_COLUMNS.items():
            col_defs = ", ".join(f"{c} TEXT" for c in cols)
            conn.execute(f"CREATE TABLE {table} ({col_defs})")
        conn.commit()
        conn.close()
        server = _viewer.create_server(db_path, port=0)
        server.server_close()


# ---------------------------------------------------------------------------
# C1 — read-only connection hardening
# ---------------------------------------------------------------------------

class TestReadOnlyConnectionHardening:
    def test_query_only_pragma_is_enabled(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        conn = _viewer._open_conn(db_path)
        try:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        finally:
            conn.close()

    def test_write_attempt_still_raises(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        conn = _viewer._open_conn(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO facts (content, namespace) VALUES ('x', '')")
        finally:
            conn.close()

    def test_open_conn_never_creates_a_missing_database(self, tmp_path):
        missing = tmp_path / "does_not_exist.db"
        with pytest.raises(FileNotFoundError):
            _viewer._open_conn(missing)
        assert not missing.exists()


# ---------------------------------------------------------------------------
# C2 — connection lifecycle: every opened connection is actually closed
# ---------------------------------------------------------------------------

def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


class TestConnectionLifecycle:
    def test_every_request_connection_is_closed(self, tmp_path, monkeypatch):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns = "telegram:chat_LIFECYCLE"
        store.add_fact("A fact for lifecycle testing.", category="general", namespace=ns)
        store.close()

        opened: list[sqlite3.Connection] = []
        original_open_conn = _viewer._open_conn

        def _tracking_open_conn(path):
            conn = original_open_conn(path)
            opened.append(conn)
            return conn

        monkeypatch.setattr(_viewer, "_open_conn", _tracking_open_conn)

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            for path in ("/", f"/namespace?ns={digest}", "/does-not-exist"):
                s.get(path)
        finally:
            s.stop()

        assert len(opened) >= 3
        assert all(_is_closed(c) for c in opened)

    def test_connection_is_closed_even_when_the_request_errors(self, tmp_path, monkeypatch):
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()

        opened: list[sqlite3.Connection] = []
        original_open_conn = _viewer._open_conn

        def _tracking_open_conn(path):
            conn = original_open_conn(path)
            opened.append(conn)
            return conn

        monkeypatch.setattr(_viewer, "_open_conn", _tracking_open_conn)

        s = _start_server(db_path)
        try:
            # Malformed digest/id still reaches _dispatch's `with
            # _connection(...)` block and returns a 400/404 — not an
            # exception — but confirms the context manager closes the
            # connection on every one of these ordinary early-return paths.
            s.get("/fact?ns=&id=not-a-number")
            s.get("/entity?ns=0000&id=1")
        finally:
            s.stop()

        assert len(opened) >= 2
        assert all(_is_closed(c) for c in opened)


# ---------------------------------------------------------------------------
# C3/C5 — WAL concurrency with a real writer
# ---------------------------------------------------------------------------

class TestWalConcurrency:
    def test_viewer_coexists_with_a_concurrent_writer(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        ns = "telegram:chat_WAL"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("Fact present before the server starts.", category="general", namespace=ns)
        # Confirm this database really is in WAL mode, matching production.
        assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

        s = _start_server(db_path)
        digest = _namespace_digest(ns)
        errors: list[Exception] = []

        def _writer() -> None:
            try:
                for i in range(20):
                    store.add_fact(f"Concurrently written fact {i}.", category="general", namespace=ns)
                    time.sleep(0.005)
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                errors.append(exc)

        writer_thread = threading.Thread(target=_writer)
        writer_thread.start()
        try:
            bad_statuses = []
            for _ in range(40):
                resp = s.get(f"/namespace?ns={digest}")
                if resp.status != 200:
                    bad_statuses.append(resp.status)
                time.sleep(0.003)
        finally:
            writer_thread.join(timeout=10)
            s.stop()
            store.close()

        assert not errors, f"writer thread raised: {errors}"
        assert not writer_thread.is_alive()
        assert bad_statuses == []

    def test_read_after_write_visibility_through_a_fresh_connection(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        ns = "telegram:chat_WAL_VISIBILITY"
        store = MemoryStore(db_path=db_path, hrr_dim=32)

        s = _start_server(db_path)
        try:
            digest = _namespace_digest(ns)
            resp_before = s.get(f"/namespace?ns={digest}")
            assert resp_before.status == 404  # namespace doesn't exist yet

            store.add_fact("Written after the viewer started.", category="general", namespace=ns)

            resp_after = s.get(f"/namespace?ns={digest}")
            assert resp_after.status == 200
            assert "Written after the viewer started" in resp_after.body.decode("utf-8")
        finally:
            s.stop()
            store.close()

    def test_server_shuts_down_cleanly_after_concurrent_access(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("fact", category="general", namespace="telegram:chat_SHUTDOWN")
        store.close()

        s = _start_server(db_path)
        for _ in range(10):
            s.get("/")
        s.stop()  # must not hang
        assert not s.thread.is_alive()
