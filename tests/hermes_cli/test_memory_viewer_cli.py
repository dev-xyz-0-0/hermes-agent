"""Phase 7 — `hermes memory viewer` CLI wiring tests.

See docs/plans/demo-reference-plan/checklist.md section 11 ("Phase 7 —
Optional local viewer"). Companion to
tests/plugins/memory/test_holographic_viewer.py (HTTP-level behavior) and
tests/plugins/memory/test_holographic_viewer_optional_import.py (optional
import). Uses the repo-wide `_isolate_hermes_home` autouse fixture
(tests/conftest.py) so HERMES_HOME is always a temp directory, never the
real ~/.hermes/.
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from hermes_constants import get_hermes_home


def _ns(**kwargs):
    defaults = {"json": False, "port": 0, "host": "127.0.0.1", "show_raw_identity": False}
    defaults.update(kwargs)
    return Namespace(**defaults)


def _seed_db():
    from plugins.memory.holographic.store import MemoryStore

    db_path = get_hermes_home() / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    store.add_fact("A fact for viewer CLI testing.", category="general")
    store.close()
    return db_path


class TestCmdViewerMissingDatabase:
    def test_missing_database_exits_nonzero_without_starting_a_server(self, capsys):
        from hermes_cli.memory_setup import cmd_viewer

        with pytest.raises(SystemExit) as exc_info:
            cmd_viewer(_ns())
        assert exc_info.value.code != 0
        out = capsys.readouterr().out
        assert "not found" in out


class TestCmdViewerNonLoopbackHost:
    def test_non_loopback_host_is_refused_before_serving(self, capsys):
        from hermes_cli.memory_setup import cmd_viewer

        _seed_db()
        with pytest.raises(SystemExit) as exc_info:
            cmd_viewer(_ns(host="0.0.0.0"))
        assert exc_info.value.code != 0
        out = capsys.readouterr().out
        assert "not supported" in out.lower()


class TestCmdViewerStartsAndStops:
    def test_viewer_binds_loopback_and_can_be_interrupted(self, capsys, monkeypatch):
        """Exercise the real `cmd_viewer` path end-to-end: it must
        construct a real loopback-bound server and then return cleanly
        when `serve_forever` raises KeyboardInterrupt (simulating Ctrl+C),
        rather than hanging or crashing."""
        from hermes_cli.memory_setup import cmd_viewer

        _seed_db()

        from plugins.memory.holographic import viewer as _viewer

        original_create_server = _viewer.create_server
        created = {}

        def _tracking_create_server(*args, **kwargs):
            server = original_create_server(*args, **kwargs)
            created["server"] = server

            # Simulate Ctrl+C without ever entering the real polling loop —
            # stubbing serve_forever() this way means shutdown()'s internal
            # "wait until the loop's finally block sets this event" dance
            # never runs either, so shutdown()/server_close() must be
            # stubbed too (this test only needs to prove cmd_viewer binds
            # loopback and handles KeyboardInterrupt cleanly, not exercise
            # socketserver's own shutdown protocol).
            server.serve_forever = lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt())
            server.shutdown = lambda: None
            real_close = server.server_close
            server.server_close = lambda: real_close()
            return server

        monkeypatch.setattr(_viewer, "create_server", _tracking_create_server)

        cmd_viewer(_ns(port=0))

        assert "server" in created
        assert created["server"].server_address[0] == "127.0.0.1"
        out = capsys.readouterr().out
        assert "Read-only" in out
        assert "Serving" in out


class TestMemoryCommandRouterWiring:
    def test_viewer_subcommand_routes_to_cmd_viewer(self, monkeypatch):
        from hermes_cli import memory_setup

        called = {}
        monkeypatch.setattr(memory_setup, "cmd_viewer", lambda args: called.setdefault("ok", True))
        memory_setup.memory_command(Namespace(memory_command="viewer"))
        assert called.get("ok") is True

    def test_viewer_is_registered_in_the_argparse_subparsers(self):
        """`hermes memory viewer --help` must exist via the central
        argparse wiring in hermes_cli/main.py — exercised the same way the
        existing health/benchmark/rebuild-vectors subcommands are."""
        import subprocess
        import sys
        from pathlib import Path

        project_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "memory", "viewer", "--help"],
            cwd=str(project_root), capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "--port" in result.stdout
        assert "--host" in result.stdout
        assert "--show-raw-identity" in result.stdout

    def test_viewer_not_a_chat_slash_command(self):
        """Must never be reachable via COMMAND_REGISTRY (chat slash
        commands) or gateway dispatch — same guarantee as every other
        `hermes memory` maintenance-style subcommand."""
        from hermes_cli.commands import COMMAND_REGISTRY

        names = {c.name for c in COMMAND_REGISTRY}
        for forbidden in ("viewer", "memory-viewer"):
            assert forbidden not in names
