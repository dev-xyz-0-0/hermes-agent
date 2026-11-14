"""Phase 7.1F — correctness and lifecycle tests for the Holographic memory
viewer (`plugins/memory/holographic/viewer.py`).

Covers status-semantics correctness (F2 — unknown statuses must never be
auto-classified as active) and server lifecycle (F4 — clean startup
message, graceful shutdown, bind-failure handling surfaces a clear error
rather than a traceback). F1 (supersession chain scope) and F5 (code
organization) are documentation-only — no behavior to test. F3
(relationship metadata) is already covered by the redaction/namespace
suites.
"""

from __future__ import annotations

import socket
import threading

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server  # noqa: F401


# ---------------------------------------------------------------------------
# F2 — unknown status is never silently classified as active
# ---------------------------------------------------------------------------

class TestUnknownStatusSemantics:
    @pytest.fixture
    def db_with_weird_status(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        ns = "telegram:chat_WEIRD_STATUS"
        store.add_fact("A normal active fact.", category="general", namespace=ns)
        old_id = store.add_fact("An old fact to supersede.", category="general", namespace=ns)
        store.supersede_fact(old_id, "A superseding fact.", namespace=ns)
        # No real code path produces this — simulating a hand-edited row /
        # migration bug / future status value, which is exactly what F2
        # must defend against regardless of how it got there.
        store._conn.execute(
            "UPDATE facts SET status = 'quarantined' WHERE content = 'A normal active fact.'"
        )
        store._conn.commit()
        store.close()
        return db_path, ns

    def test_namespace_index_reports_an_unknown_bucket_not_folded_into_active(self, db_with_weird_status):
        db_path, ns = db_with_weird_status
        s = _start_server(db_path)
        try:
            resp = s.get("/")
            body = resp.body.decode("utf-8")
            assert "status-unknown" in body
            # total (3) == active (1) + superseded (1) + unknown (1)
            assert ">3<" in body
        finally:
            s.stop()

    def test_fact_listing_badge_for_unknown_status_is_not_the_active_style(self, db_with_weird_status):
        db_path, ns = db_with_weird_status
        digest = _namespace_digest(ns)
        s = _start_server(db_path)
        try:
            resp = s.get(f"/namespace?ns={digest}")
            body = resp.body.decode("utf-8")
            assert "class=\"badge status-unknown\"" in body
            assert ">quarantined<" in body
        finally:
            s.stop()

    def test_fact_detail_badge_for_unknown_status_is_not_the_active_style(self, db_with_weird_status):
        db_path, ns = db_with_weird_status
        digest = _namespace_digest(ns)
        s = _start_server(db_path)
        try:
            resp = s.get(f"/namespace?ns={digest}")
            body = resp.body.decode("utf-8")
            import re
            m = re.search(r"/fact\?ns=" + re.escape(digest) + r"&id=(\d+)[^<]*</a></td><td><span class=\"badge status-unknown\"", body)
            assert m, "expected the quarantined fact's row to link a fact id next to the unknown badge"
            detail = s.get(f"/fact?ns={digest}&id={m.group(1)}")
            detail_body = detail.body.decode("utf-8")
            assert "class=\"badge status-unknown\"" in detail_body
        finally:
            s.stop()

    def test_normal_active_and_superseded_facts_are_unaffected(self, db_with_weird_status):
        """The F2 fix must not reclassify ordinary active/superseded
        facts — only genuinely unrecognized status values move."""
        db_path, ns = db_with_weird_status
        digest = _namespace_digest(ns)
        s = _start_server(db_path)
        try:
            resp = s.get(f"/namespace?ns={digest}")
            body = resp.body.decode("utf-8")
            assert "status-active" in body
            assert "status-superseded" in body
        finally:
            s.stop()


# ---------------------------------------------------------------------------
# F4 — server lifecycle: clean startup message, graceful shutdown,
# bind-failure handling
# ---------------------------------------------------------------------------

class TestServerLifecycle:
    def test_clean_startup_message_names_database_and_read_only(self, tmp_path, capsys, monkeypatch):
        from argparse import Namespace

        from hermes_cli.memory_setup import cmd_viewer

        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        monkeypatch.setattr("hermes_cli.memory_setup._holographic_db_path", lambda: db_path)

        from plugins.memory.holographic import viewer as _v

        original_create_server = _v.create_server

        def _tracking(*args, **kwargs):
            server = original_create_server(*args, **kwargs)
            server.serve_forever = lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt())
            server.shutdown = lambda: None
            return server

        monkeypatch.setattr(_v, "create_server", _tracking)

        cmd_viewer(Namespace(port=0, host="127.0.0.1", show_raw_identity=False, json=False))
        out = capsys.readouterr().out
        assert "Read-only" in out
        assert "Serving" in out
        assert "Press Ctrl+C to stop" in out
        assert str(db_path) in out

    def test_bind_failure_on_an_already_used_port_gives_a_clear_message_not_a_traceback(self, tmp_path, capsys, monkeypatch):
        from argparse import Namespace

        from hermes_cli.memory_setup import cmd_viewer

        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        monkeypatch.setattr("hermes_cli.memory_setup._holographic_db_path", lambda: db_path)

        occupying_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupying_socket.bind(("127.0.0.1", 0))
        occupying_socket.listen(1)
        busy_port = occupying_socket.getsockname()[1]
        try:
            with pytest.raises(SystemExit) as exc_info:
                cmd_viewer(Namespace(port=busy_port, host="127.0.0.1", show_raw_identity=False, json=False))
            assert exc_info.value.code != 0
            out = capsys.readouterr().out
            assert "Could not bind" in out
            assert "Traceback" not in out
        finally:
            occupying_socket.close()

    def test_unsupported_schema_at_startup_gives_a_clear_message_not_a_traceback(self, tmp_path, capsys, monkeypatch):
        from argparse import Namespace

        from hermes_cli.memory_setup import cmd_viewer

        db_path = tmp_path / "bad_schema.db"
        db_path.write_bytes(b"not a real sqlite database")
        monkeypatch.setattr("hermes_cli.memory_setup._holographic_db_path", lambda: db_path)

        with pytest.raises(SystemExit) as exc_info:
            cmd_viewer(Namespace(port=0, host="127.0.0.1", show_raw_identity=False, json=False))
        assert exc_info.value.code != 0
        out = capsys.readouterr().out
        assert "valid SQLite database" in out
        assert "Traceback" not in out

    def test_graceful_shutdown_closes_the_listening_socket(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        s = _start_server(db_path)
        port = s.port
        s.get("/")
        s.stop()
        assert not s.thread.is_alive()
        # The port should be free again immediately — no orphaned listener.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()

    def test_worker_threads_are_daemonic_so_they_never_block_process_exit(self, tmp_path):
        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        server = _viewer.create_server(db_path, port=0)
        try:
            assert server.daemon_threads is True
        finally:
            server.server_close()
