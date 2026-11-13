"""Phase 6 — `hermes memory rebuild-vectors|rebuild-banks|
backfill-relationships|verify` CLI tests.

See docs/plans/demo-reference-plan/checklist.md section 9 ("Phase 6 —
Maintenance commands" and its "Maintenance tests" subsection).
Uses the repo-wide `_isolate_hermes_home` autouse fixture (tests/conftest.py)
so HERMES_HOME is always a temp directory here, never the real ~/.hermes/.
"""

from __future__ import annotations

import hashlib
import json
from argparse import Namespace

import pytest

from hermes_constants import get_hermes_home


def _ns(**kwargs):
    defaults = {
        "json": False, "dry_run": True, "apply": False, "backup_path": None,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _seed_db():
    from plugins.memory.holographic.store import MemoryStore

    db_path = get_hermes_home() / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    store.add_fact("A fact for CLI maintenance testing.", category="general")
    store.close()
    return db_path


def _file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestRebuildVectorsCommand:
    def test_missing_database_exits_cannot_run(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_vectors

        with pytest.raises(SystemExit) as exc_info:
            cmd_rebuild_vectors(_ns(json=True, dry_run=True))
        assert exc_info.value.code == 3

    def test_dry_run_changes_no_files(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_vectors

        db_path = _seed_db()
        before = _file_hash(db_path)
        with pytest.raises(SystemExit) as exc_info:
            cmd_rebuild_vectors(_ns(json=True, dry_run=True))
        assert exc_info.value.code == 0
        after = _file_hash(db_path)
        assert before == after
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["dry_run"] is True
        assert parsed["db_path"] == str(db_path)

    def test_apply_mode_resolves_target_path(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_vectors

        _seed_db()
        with pytest.raises(SystemExit) as exc_info:
            cmd_rebuild_vectors(_ns(json=True, dry_run=False))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["dry_run"] is False
        assert parsed["ok"] is True

    def test_json_mode_has_no_decorative_text(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_vectors

        _seed_db()
        with pytest.raises(SystemExit):
            cmd_rebuild_vectors(_ns(json=True, dry_run=True))
        out = capsys.readouterr().out.strip()
        json.loads(out)


class TestRebuildBanksCommand:
    def test_dry_run_changes_no_files(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_banks

        db_path = _seed_db()
        before = _file_hash(db_path)
        with pytest.raises(SystemExit) as exc_info:
            cmd_rebuild_banks(_ns(json=True, dry_run=True))
        assert exc_info.value.code == 0
        after = _file_hash(db_path)
        assert before == after

    def test_apply_succeeds(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_banks

        _seed_db()
        with pytest.raises(SystemExit) as exc_info:
            cmd_rebuild_banks(_ns(json=True, dry_run=False))
        assert exc_info.value.code == 0


class TestBackfillRelationshipsCommand:
    def test_requires_dry_run_or_apply_flag(self, capsys):
        from hermes_cli.memory_setup import cmd_backfill_relationships

        _seed_db()
        with pytest.raises(SystemExit) as exc_info:
            cmd_backfill_relationships(_ns(json=True, dry_run=False, apply=False))
        assert exc_info.value.code == 3
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["ok"] is False

    def test_dry_run_writes_zero_relationships(self, capsys):
        from hermes_cli.memory_setup import cmd_backfill_relationships
        from plugins.memory.holographic.store import MemoryStore

        db_path = get_hermes_home() / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        alice = store._resolve_entity("Alice")
        bob = store._resolve_entity("Bob")
        fid = store.add_fact("Alice and Bob worked together.", category="general")
        store._link_fact_entity(fid, alice)
        store._link_fact_entity(fid, bob)
        store.close()

        with pytest.raises(SystemExit) as exc_info:
            cmd_backfill_relationships(_ns(json=True, dry_run=True, apply=False))
        assert exc_info.value.code == 0

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            rows = store.read("SELECT COUNT(*) FROM entity_relationships")
            assert rows[0][0] == 0
        finally:
            store.close()

    def test_apply_writes_relationships(self, capsys):
        from hermes_cli.memory_setup import cmd_backfill_relationships
        from plugins.memory.holographic.store import MemoryStore

        db_path = get_hermes_home() / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        alice = store._resolve_entity("Alice")
        bob = store._resolve_entity("Bob")
        fid = store.add_fact("Alice and Bob worked together.", category="general")
        store._link_fact_entity(fid, alice)
        store._link_fact_entity(fid, bob)
        store.close()

        with pytest.raises(SystemExit) as exc_info:
            cmd_backfill_relationships(_ns(json=True, dry_run=False, apply=True))
        assert exc_info.value.code == 0

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            rows = store.read("SELECT COUNT(*) FROM entity_relationships")
            assert rows[0][0] == 1
        finally:
            store.close()


class TestVerifyCommand:
    def test_missing_database_exits_cannot_run(self, capsys):
        from hermes_cli.memory_setup import cmd_verify

        with pytest.raises(SystemExit) as exc_info:
            cmd_verify(_ns(json=True))
        assert exc_info.value.code != 0

    def test_healthy_db_exits_zero(self, capsys):
        from hermes_cli.memory_setup import cmd_verify

        _seed_db()
        with pytest.raises(SystemExit) as exc_info:
            cmd_verify(_ns(json=True))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["ok"] is True

    def test_json_mode_has_no_decorative_text(self, capsys):
        from hermes_cli.memory_setup import cmd_verify

        _seed_db()
        with pytest.raises(SystemExit):
            cmd_verify(_ns(json=True))
        out = capsys.readouterr().out.strip()
        json.loads(out)


class TestNoRawIdentityOrSecretInOutput:
    def test_text_mode_never_shows_raw_namespace_or_content(self, capsys):
        from hermes_cli.memory_setup import cmd_rebuild_vectors
        from plugins.memory.holographic.store import MemoryStore

        db_path = get_hermes_home() / "memory_store.db"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("A secret project called Nightjar.", category="project", namespace="telegram:9999")
        store.close()

        with pytest.raises(SystemExit):
            cmd_rebuild_vectors(_ns(json=False, dry_run=False))
        out = capsys.readouterr().out
        assert "telegram:9999" not in out
        assert "Nightjar" not in out


class TestMemoryMaintenanceCommandRegistryWiring:
    def test_maintenance_commands_are_not_chat_slash_commands(self):
        """These commands must never be exposed via COMMAND_REGISTRY (chat
        slash commands) or gateway dispatch — see
        docs/plans/demo-reference-plan/checklist.md section 9."""
        from hermes_cli.commands import COMMAND_REGISTRY

        names = {c.name for c in COMMAND_REGISTRY}
        for forbidden in (
            "rebuild-vectors", "rebuild-banks", "backfill-relationships",
            "verify", "memory-rebuild-vectors", "memory-rebuild-banks",
            "memory-backfill-relationships", "memory-verify",
        ):
            assert forbidden not in names

    def test_maintenance_commands_are_not_model_tools(self):
        """Never wired into tools/registry.py — maintenance is CLI-only."""
        from tools.registry import registry

        tool_names = {entry.name for entry in registry._snapshot_entries()}
        for forbidden in ("rebuild_vectors", "rebuild_banks", "backfill_relationships"):
            assert forbidden not in tool_names

    def test_cli_commands_have_no_namespace_argument(self):
        import inspect

        from hermes_cli.memory_setup import (
            cmd_backfill_relationships,
            cmd_rebuild_banks,
            cmd_rebuild_vectors,
            cmd_verify,
        )

        for fn in (cmd_rebuild_vectors, cmd_rebuild_banks, cmd_backfill_relationships, cmd_verify):
            source = inspect.getsource(fn)
            assert "args.namespace" not in source
            assert "args.chat_id" not in source
