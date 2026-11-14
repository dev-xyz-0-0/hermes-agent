"""P1 — `hermes memory restore` service-layer tests.

See docs plan "Holographic Memory P1 Fix Plan — `hermes memory restore`
CLI". These tests exercise `maintenance.restore()` directly (the service
layer), independent of the CLI wrapper covered in
tests/hermes_cli/test_memory_restore_cli.py.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from plugins.memory.holographic import backup as backup_module
from plugins.memory.holographic import diagnostics, maintenance
from plugins.memory.holographic.store import MemoryStore


def _file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_backup(tmp_path, name, *, facts, bank_warning=False):
    """Build a standalone db with ``facts`` and return its verified backup info."""
    src_path = tmp_path / f"{name}_source.sqlite"
    store = MemoryStore(db_path=src_path, hrr_dim=32)
    for content, category, namespace in facts:
        store.add_fact(content, category=category, namespace=namespace)
    if bank_warning:
        store._conn.execute("UPDATE memory_banks SET fact_count = fact_count + 5")
        store._conn.commit()
    store.close()
    return backup_module.create_backup(str(src_path), str(tmp_path / f"{name}_backups"))


def _make_live(tmp_path, *, facts):
    db_path = tmp_path / "live.sqlite"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    for content, category, namespace in facts:
        store.add_fact(content, category=category, namespace=namespace)
    store.close()
    return db_path


# ---------------------------------------------------------------------------
# Task 1 — dry-run plan is read-only and reports a correct diff/gates
# ---------------------------------------------------------------------------

class TestRestoreDryRun:
    def test_dry_run_is_read_only_and_reports_diff(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[
            ("live fact one", "general", ""),
            ("live fact two", "general", ""),
        ])
        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])

        before_hash = _file_hash(live_path)

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
        )

        after_hash = _file_hash(live_path)
        assert before_hash == after_hash

        assert result.dry_run is True
        d = result.to_dict()
        assert d["backup_verify"]["ok"] is True
        assert d["diff"]["facts"]["backup"] == 1
        assert d["diff"]["facts"]["live"] == 2
        assert d["diff"]["facts"]["delta"] == -1
        assert d["gates"]["apply_allowed"] is True
        assert d["gates"]["block_reasons"] == []
        assert result.exit_code == maintenance.EXIT_OK

    def test_dry_run_reports_tombstone_count(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        store = MemoryStore(db_path=live_path, hrr_dim=32)
        row = store._conn.execute("SELECT fact_id FROM facts").fetchone()
        store.remove_fact(row[0])
        store.close()

        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
        )
        assert result.to_dict()["tombstones_live_count"] == 1
        assert result.to_dict()["would_block_reactivation"] is True

    def test_dry_run_against_nonexistent_live_db_still_plans(self, tmp_path):
        """Restoring onto a brand-new profile that has no live DB yet must
        not crash — there's simply nothing to diff against."""
        live_path = tmp_path / "does_not_exist.sqlite"
        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
        )
        assert result.dry_run is True
        assert result.to_dict()["live_health"] is None
        assert result.to_dict()["gates"]["apply_allowed"] is True


# ---------------------------------------------------------------------------
# Task 3 — apply refuses invalid/unhealthy backups
# ---------------------------------------------------------------------------

class TestRestoreGatesRefuseInvalidBackups:
    def test_missing_backup_path_refused(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        before_hash = _file_hash(live_path)

        result = maintenance.restore(
            live_path, backup_path=tmp_path / "nope.db", hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_CANNOT_RUN
        assert _file_hash(live_path) == before_hash

    def test_missing_sidecar_refused(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])
        os.remove(info.path + ".json")
        before_hash = _file_hash(live_path)

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_ERRORS
        assert _file_hash(live_path) == before_hash

    def test_checksum_mismatch_refused(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])
        with open(info.path, "r+b") as f:
            f.seek(50)
            f.write(b"\xff" * 16)
        before_hash = _file_hash(live_path)

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_ERRORS
        assert _file_hash(live_path) == before_hash

    def test_backup_health_error_refused(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])

        src_path = tmp_path / "bad_source.sqlite"
        store = MemoryStore(db_path=src_path, hrr_dim=32)
        store.add_fact("bad fact", category="general")
        store._conn.execute(
            "UPDATE facts SET superseded_by = 999999 WHERE content = 'bad fact'"
        )
        store._conn.commit()
        store.close()
        info = backup_module.create_backup(str(src_path), str(tmp_path / "bad_backups"))

        before_hash = _file_hash(live_path)
        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_ERRORS
        assert _file_hash(live_path) == before_hash

    def test_backup_health_warning_blocks_apply_without_flag(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        info = _make_backup(tmp_path, "warn", facts=[("backup fact", "general", "")], bank_warning=True)

        before_hash = _file_hash(live_path)
        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
            allow_backup_warnings=False,
        )
        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_WARNINGS
        assert _file_hash(live_path) == before_hash

        dry = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
        )
        assert dry.to_dict()["gates"]["apply_allowed"] is False
        assert dry.to_dict()["gates"]["backup_health_warnings"] >= 1

    def test_backup_health_warning_allowed_with_flag(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        info = _make_backup(tmp_path, "warn", facts=[("backup fact", "general", "")], bank_warning=True)

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
            allow_backup_warnings=True,
        )
        assert result.ok is True
        assert result.to_dict()["gates"]["apply_allowed"] is True


# ---------------------------------------------------------------------------
# Task 4 — apply actually restores, tombstones respected, idempotent-ish
# ---------------------------------------------------------------------------

class TestRestoreApply:
    def test_apply_restores_facts_and_returns_safety_copy(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("old live fact", "general", "")])
        info = _make_backup(tmp_path, "clean", facts=[("restored fact", "general", "")])

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is True
        d = result.to_dict()
        assert d["safety_copy_path"]
        assert os.path.exists(d["safety_copy_path"])
        assert d["post_health"]["exit_code"] == diagnostics.EXIT_OK

        store = MemoryStore(db_path=live_path, hrr_dim=32)
        contents = [f["content"] for f in store.list_facts(limit=10)]
        store.close()
        assert "restored fact" in contents
        assert "old live fact" not in contents

    def test_apply_does_not_reactivate_tombstoned_fact(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[
            ("keep this fact", "general", ""),
            ("forget this fact", "general", ""),
        ])
        info = _make_backup(tmp_path, "clean", facts=[
            ("keep this fact", "general", ""),
            ("forget this fact", "general", ""),
        ])

        store = MemoryStore(db_path=live_path, hrr_dim=32)
        row = store._conn.execute(
            "SELECT fact_id FROM facts WHERE content = 'forget this fact'"
        ).fetchone()
        store.remove_fact(row[0])
        store.close()

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is True
        assert result.to_dict()["reactivated_blocked"] == 1

        store = MemoryStore(db_path=live_path, hrr_dim=32)
        contents = [f["content"] for f in store.list_facts(limit=10)]
        store.close()
        assert "forget this fact" not in contents
        assert "keep this fact" in contents

    def test_apply_on_fresh_target_with_no_existing_live_db(self, tmp_path):
        live_path = tmp_path / "fresh.sqlite"
        info = _make_backup(tmp_path, "clean", facts=[("fresh fact", "general", "")])

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=False,
        )
        assert result.ok is True
        assert result.to_dict()["safety_copy_path"] == ""

        store = MemoryStore(db_path=live_path, hrr_dim=32)
        contents = [f["content"] for f in store.list_facts(limit=10)]
        store.close()
        assert "fresh fact" in contents


# ---------------------------------------------------------------------------
# P1b — .enc refusal + gateway advisory warning
# ---------------------------------------------------------------------------

class TestRestoreP1bGuards:
    def test_enc_input_is_refused_without_touching_live(self, tmp_path):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        before = _file_hash(live_path)
        enc_path = tmp_path / "skills-pack-snapshot.enc"
        enc_path.write_bytes(b"not-a-sqlite-db")

        result = maintenance.restore(
            live_path, backup_path=enc_path, hermes_home=tmp_path, dry_run=True,
        )
        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_CANNOT_RUN
        assert result.gates["apply_allowed"] is False
        assert result.gates.get("input_format") == "skills_pack_encrypted"
        joined = " ".join(result.issues + result.gates["block_reasons"])
        assert "hermes_backup" in joined
        assert ".enc" in joined
        assert before == _file_hash(live_path)

    def test_gateway_running_is_advisory_warning_not_hard_block(self, tmp_path, monkeypatch):
        live_path = _make_live(tmp_path, facts=[("live fact", "general", "")])
        info = _make_backup(tmp_path, "clean", facts=[("backup fact", "general", "")])

        monkeypatch.setattr(maintenance, "_gateway_running_for_home", lambda _home: True)

        result = maintenance.restore(
            live_path, backup_path=info.path, hermes_home=tmp_path, dry_run=True,
        )
        d = result.to_dict()
        assert d["gates"]["gateway_running"] is True
        assert d["gates"]["apply_allowed"] is True
        assert any("gateway" in w.lower() for w in d["gates"]["warnings"])
        # Advisory only — clean backup still exits 0.
        assert result.exit_code == maintenance.EXIT_OK
