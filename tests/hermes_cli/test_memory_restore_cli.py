"""P1 — `hermes memory restore` CLI tests.

Uses the repo-wide `_isolate_hermes_home` autouse fixture (tests/conftest.py)
so HERMES_HOME is always a temp directory here, never the real ~/.hermes/.
"""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

from hermes_constants import get_hermes_home


def _ns(**kwargs):
    defaults = {
        "json": False, "dry_run": True, "allow_backup_warnings": False,
        "backup_from": None,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _seed_live(content="A fact for restore CLI testing."):
    from plugins.memory.holographic.store import MemoryStore

    db_path = get_hermes_home() / "memory_store.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    store.add_fact(content, category="general")
    store.close()
    return db_path


def _make_backup(tmp_path, content="A backed-up fact."):
    from plugins.memory.holographic import backup as backup_module
    from plugins.memory.holographic.store import MemoryStore

    src_path = tmp_path / "source.sqlite"
    store = MemoryStore(db_path=src_path, hrr_dim=32)
    store.add_fact(content, category="general")
    store.close()
    return backup_module.create_backup(str(src_path), str(tmp_path / "backups"))


class TestRestoreCommand:
    def test_dry_run_is_default(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_restore

        _seed_live()
        info = _make_backup(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            cmd_restore(_ns(json=True, dry_run=True, backup_from=info.path))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["dry_run"] is True
        assert parsed["ok"] is True

    def test_missing_from_path_refused_without_touching_live(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_restore

        db_path = _seed_live()
        import hashlib
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        with pytest.raises(SystemExit) as exc_info:
            cmd_restore(_ns(json=True, dry_run=False, backup_from=str(tmp_path / "nope.db")))
        assert exc_info.value.code == 3

        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after

    def test_apply_flag_flips_dry_run(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_restore

        _seed_live()
        info = _make_backup(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            cmd_restore(_ns(json=True, dry_run=False, backup_from=info.path))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["dry_run"] is False
        assert parsed["ok"] is True
        assert parsed["safety_copy_path"]

    def test_json_mode_has_no_decorative_text(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_restore

        _seed_live()
        info = _make_backup(tmp_path)
        with pytest.raises(SystemExit):
            cmd_restore(_ns(json=True, dry_run=True, backup_from=info.path))
        out = capsys.readouterr().out.strip()
        json.loads(out)  # must parse as a single JSON object with no leading/trailing text

    def test_human_output_says_no_changes_made_on_dry_run(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_restore

        _seed_live()
        info = _make_backup(tmp_path)
        with pytest.raises(SystemExit):
            cmd_restore(_ns(json=False, dry_run=True, backup_from=info.path))
        out = capsys.readouterr().out
        assert "No changes made" in out

    def test_restore_never_registered_as_fact_store_action(self, tmp_path):
        """Security invariant from the P1 plan: restore must stay CLI-only,
        never reachable via the model-callable fact_store tool."""
        from plugins.memory.holographic import HolographicMemoryProvider

        p = HolographicMemoryProvider()
        p.initialize(session_id="cli-sess", platform="cli", hermes_home=str(get_hermes_home()))
        try:
            schema_actions: set = set()
            for tool_schema in p.get_tool_schemas():
                if tool_schema["name"] == "fact_store":
                    schema_actions = set(tool_schema["parameters"]["properties"]["action"]["enum"])
            assert "restore" not in schema_actions
        finally:
            p.shutdown()

    def test_enc_path_refused_via_cli(self, capsys, tmp_path):
        from hermes_cli.memory_setup import cmd_restore

        _seed_live()
        enc = tmp_path / "snap.enc"
        enc.write_text("encrypted-blob", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            cmd_restore(_ns(json=True, dry_run=True, backup_from=str(enc)))
        assert exc_info.value.code == 3
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["ok"] is False
        assert parsed["gates"]["apply_allowed"] is False
        assert "hermes_backup" in " ".join(parsed.get("issues") or [])
