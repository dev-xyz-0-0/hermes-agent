"""Phase 6 — maintenance commands (plugin-level) tests.

See docs/plans/demo-reference-plan/checklist.md section 9 ("Phase 6 —
Maintenance commands" and its "Maintenance tests" subsection) and
implementation-plan.md "Phase 6 — Maintenance commands".

These tests exercise `plugins/memory/holographic/maintenance.py` directly
(the service layer), independent of the CLI wrapper in
`hermes_cli/memory_setup.py` (covered separately in
tests/hermes_cli/test_memory_maintenance_cli.py).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from plugins.memory.holographic import maintenance
from plugins.memory.holographic.store import MemoryStore


def _file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_db(tmp_path, *, with_relationship_candidates: bool = False):
    db_path = tmp_path / "db.sqlite"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    try:
        store.add_fact("A plain fact for maintenance testing.", category="general")
        store.add_fact("A project fact.", category="project", namespace="telegram:111")
        if with_relationship_candidates:
            alice = store._resolve_entity("Alice")
            bob = store._resolve_entity("Bob")
            fid = store.add_fact("Alice and Bob worked together.", category="general", namespace="ns_a")
            store._link_fact_entity(fid, alice)
            store._link_fact_entity(fid, bob)
    finally:
        store.close()
    return db_path


# ---------------------------------------------------------------------------
# Dry-run writes nothing
# ---------------------------------------------------------------------------

class TestDryRunWritesNothing:
    def test_rebuild_vectors_dry_run_does_not_modify_file(self, tmp_path):
        db_path = _seed_db(tmp_path)
        before = _file_hash(db_path)
        result = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=True)
        after = _file_hash(db_path)
        assert before == after
        assert result.ok is True
        assert result.dry_run is True
        assert result.counts["facts_planned"] == 2

    def test_rebuild_banks_dry_run_does_not_modify_file(self, tmp_path):
        db_path = _seed_db(tmp_path)
        before = _file_hash(db_path)
        result = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
        after = _file_hash(db_path)
        assert before == after
        assert result.ok is True
        assert result.counts["pairs_planned"] >= 1

    def test_backfill_relationships_dry_run_writes_zero_relationships(self, tmp_path):
        db_path = _seed_db(tmp_path, with_relationship_candidates=True)
        before = _file_hash(db_path)
        result = maintenance.backfill_relationships(db_path, hermes_home=tmp_path, apply=False)
        after = _file_hash(db_path)
        assert before == after
        assert result.dry_run is True
        assert result.counts["candidates_planned"] == 1

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            rows = store.read("SELECT COUNT(*) FROM entity_relationships")
            assert rows[0][0] == 0
        finally:
            store.close()

    def test_rebuild_vectors_dry_run_no_backup_created(self, tmp_path):
        db_path = _seed_db(tmp_path)
        maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=True)
        assert not (tmp_path / "backups").exists()


# ---------------------------------------------------------------------------
# Apply requires explicit flag
# ---------------------------------------------------------------------------

class TestApplyRequiresExplicitFlag:
    def test_backfill_relationships_default_is_dry_run_like(self, tmp_path):
        """The service itself defaults apply=False; the CLI layer additionally
        refuses to run with neither --dry-run nor --apply (see
        test_memory_maintenance_cli.py)."""
        db_path = _seed_db(tmp_path, with_relationship_candidates=True)
        result = maintenance.backfill_relationships(db_path, hermes_home=tmp_path)
        assert result.dry_run is True

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            rows = store.read("SELECT COUNT(*) FROM entity_relationships")
            assert rows[0][0] == 0
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Apply aborts when backup verification fails
# ---------------------------------------------------------------------------

class TestApplyAbortsOnBackupFailure:
    def test_corrupted_sidecar_checksum_blocks_apply(self, tmp_path, monkeypatch):
        db_path = _seed_db(tmp_path)

        from plugins.memory.holographic import backup as backup_module

        real_create_backup = backup_module.create_backup

        def _tampering_create_backup(db_path_arg, backup_dir):
            info = real_create_backup(db_path_arg, backup_dir)
            # Corrupt the sidecar checksum so verify_backup() fails.
            with open(info.sidecar_path, "r", encoding="utf-8") as f:
                sidecar = json.load(f)
            sidecar["checksum_sha256"] = "0" * 64
            with open(info.sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar, f)
            return info

        monkeypatch.setattr(maintenance.backup_module, "create_backup", _tampering_create_backup)

        before_hash = _file_hash(db_path)
        result = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=False)
        after_hash = _file_hash(db_path)

        assert result.ok is False
        assert result.exit_code == maintenance.EXIT_ERRORS
        assert "backup verification failed" in result.issues[0]
        # No write happened to the live DB.
        assert before_hash == after_hash


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

class TestMaintenanceLock:
    def test_concurrent_holders_second_fails_cleanly(self, tmp_path):
        db_path = _seed_db(tmp_path)
        errors = []

        def _hold_lock():
            with maintenance.maintenance_lock(db_path):
                time.sleep(0.3)

        t = threading.Thread(target=_hold_lock)
        t.start()
        time.sleep(0.05)
        try:
            with pytest.raises(maintenance.MaintenanceLockError):
                with maintenance.maintenance_lock(db_path):
                    pass
        finally:
            t.join()

    def test_different_db_paths_do_not_contend(self, tmp_path):
        db_path_a = tmp_path / "a.sqlite"
        db_path_b = tmp_path / "b.sqlite"
        db_path_a.write_bytes(b"")
        db_path_b.write_bytes(b"")

        with maintenance.maintenance_lock(db_path_a):
            # Must not raise — different database, different lock file.
            with maintenance.maintenance_lock(db_path_b):
                pass

    def test_lock_released_after_context_exits(self, tmp_path):
        db_path = _seed_db(tmp_path)
        with maintenance.maintenance_lock(db_path):
            pass
        # A second, later acquisition must succeed since the first was released.
        with maintenance.maintenance_lock(db_path):
            pass


# ---------------------------------------------------------------------------
# Interruption / idempotent restart
# ---------------------------------------------------------------------------

class TestInterruptionAndIdempotentRestart:
    def test_rebuild_vectors_twice_is_idempotent(self, tmp_path):
        db_path = _seed_db(tmp_path)
        r1 = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=False)
        assert r1.ok is True

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            vectors_after_first = [row["hrr_vector"] for row in store.read("SELECT hrr_vector FROM facts ORDER BY fact_id")]
        finally:
            store.close()

        r2 = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=False)
        assert r2.ok is True

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            vectors_after_second = [row["hrr_vector"] for row in store.read("SELECT hrr_vector FROM facts ORDER BY fact_id")]
        finally:
            store.close()

        assert vectors_after_first == vectors_after_second

    def test_rebuild_banks_twice_is_idempotent(self, tmp_path):
        db_path = _seed_db(tmp_path)
        maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=False)

        r1 = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=False)
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            banks_after_first = sorted(
                (row["bank_name"], row["vector"], row["fact_count"])
                for row in store.read("SELECT bank_name, vector, fact_count FROM memory_banks")
            )
        finally:
            store.close()

        r2 = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=False)
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            banks_after_second = sorted(
                (row["bank_name"], row["vector"], row["fact_count"])
                for row in store.read("SELECT bank_name, vector, fact_count FROM memory_banks")
            )
        finally:
            store.close()

        assert r1.ok is True and r2.ok is True
        assert banks_after_first == banks_after_second

    def test_backfill_relationships_rerun_after_simulated_interruption(self, tmp_path):
        """Simulate an interruption: apply the backfill once, then apply it
        again (as a restart would). The upsert-safe canonicalized pair
        ordering must produce the exact same single edge both times, not a
        duplicate/mirrored edge."""
        db_path = _seed_db(tmp_path, with_relationship_candidates=True)

        r1 = maintenance.backfill_relationships(db_path, hermes_home=tmp_path, apply=True)
        assert r1.ok is True
        assert r1.counts["relationships_written"] == 1

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            rows_after_first = store.read(
                "SELECT source_entity_id, target_entity_id, relationship_type, namespace FROM entity_relationships"
            )
        finally:
            store.close()
        assert len(rows_after_first) == 1

        # "Restart": run apply again.
        r2 = maintenance.backfill_relationships(db_path, hermes_home=tmp_path, apply=True)
        assert r2.ok is True

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            rows_after_second = store.read(
                "SELECT source_entity_id, target_entity_id, relationship_type, namespace FROM entity_relationships"
            )
        finally:
            store.close()

        assert len(rows_after_second) == 1
        assert list(rows_after_first) == list(rows_after_second)


# ---------------------------------------------------------------------------
# rebuild-banks orphan pruning (full reconcile, not just rebuild-existing)
# ---------------------------------------------------------------------------

class TestRebuildBanksOrphanPrune:
    def test_rebuild_banks_deletes_orphan_unscoped_banks(self, tmp_path):
        from plugins.memory.holographic import diagnostics
        from plugins.memory.holographic.store import bank_name

        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        # Active namespaced fact only — this bank has no corresponding
        # active unscoped facts, so the unscoped bank below is a pure orphan.
        store.add_fact("ns fact", category="general", namespace="telegram:1")
        store._conn.execute(
            "INSERT INTO memory_banks (bank_name, vector, dim, fact_count) VALUES (?, ?, ?, ?)",
            (bank_name("", "general"), b"\x00" * (32 * 8), 32, 5),
        )
        store._conn.commit()
        store.close()

        before = diagnostics.run_diagnostics(db_path)
        assert diagnostics.CODE_BANK_COUNT_MISMATCH in {i.code for i in before.issues}

        dry = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
        assert dry.counts.get("banks_to_delete", 0) >= 1

        result = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=False)
        assert result.ok is True
        assert result.counts.get("banks_deleted", 0) >= 1

        after = diagnostics.run_diagnostics(db_path)
        assert diagnostics.CODE_BANK_COUNT_MISMATCH not in {i.code for i in after.issues}

        store2 = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            names = {row["bank_name"] for row in store2.read("SELECT bank_name FROM memory_banks")}
        finally:
            store2.close()
        assert bank_name("", "general") not in names

    def test_rebuild_banks_apply_twice_is_idempotent_after_prune(self, tmp_path):
        from plugins.memory.holographic.store import bank_name

        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("ns fact", category="general", namespace="telegram:1")
        store._conn.execute(
            "INSERT INTO memory_banks (bank_name, vector, dim, fact_count) VALUES (?, ?, ?, ?)",
            (bank_name("", "general"), b"\x00" * (32 * 8), 32, 5),
        )
        store._conn.commit()
        store.close()

        r1 = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=False)
        assert r1.counts.get("banks_deleted", 0) >= 1

        r2 = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=False)
        assert r2.ok is True
        assert r2.counts.get("banks_deleted", 0) == 0

        dry_after = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
        assert dry_after.counts.get("banks_to_delete", 0) == 0

    def test_rebuild_banks_deletes_orphan_namespaced_bank(self, tmp_path):
        """A namespaced orphan (`cat:ns:<digest>:<category>`) with no active
        pair behind it must also be deleted — not just unscoped orphans."""
        from plugins.memory.holographic.store import bank_name

        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("live fact", category="general", namespace="telegram:1")
        orphan_name = bank_name("telegram:999-gone", "tool")
        store._conn.execute(
            "INSERT INTO memory_banks (bank_name, vector, dim, fact_count) VALUES (?, ?, ?, ?)",
            (orphan_name, b"\x00" * (32 * 8), 32, 3),
        )
        store._conn.commit()
        store.close()

        dry = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
        assert dry.counts.get("banks_to_delete", 0) >= 1

        result = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=False)
        assert result.counts.get("banks_deleted", 0) >= 1

        store2 = MemoryStore(db_path=db_path, hrr_dim=32)
        try:
            names = {row["bank_name"] for row in store2.read("SELECT bank_name FROM memory_banks")}
        finally:
            store2.close()
        assert orphan_name not in names

    def test_rebuild_banks_dry_run_delete_count_does_not_modify_file(self, tmp_path):
        from plugins.memory.holographic.store import bank_name

        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("ns fact", category="general", namespace="telegram:1")
        store._conn.execute(
            "INSERT INTO memory_banks (bank_name, vector, dim, fact_count) VALUES (?, ?, ?, ?)",
            (bank_name("", "general"), b"\x00" * (32 * 8), 32, 5),
        )
        store._conn.commit()
        store.close()

        before = _file_hash(db_path)
        dry = maintenance.rebuild_banks(db_path, hermes_home=tmp_path, dry_run=True)
        after = _file_hash(db_path)
        assert before == after
        assert dry.counts.get("banks_to_delete", 0) >= 1


# ---------------------------------------------------------------------------
# Named profile cannot touch another profile
# ---------------------------------------------------------------------------

class TestProfileIsolation:
    def test_apply_on_one_profile_db_never_touches_the_other(self, tmp_path):
        profile_a_home = tmp_path / "profile_a"
        profile_b_home = tmp_path / "profile_b"
        profile_a_home.mkdir()
        profile_b_home.mkdir()

        db_a = _seed_db(profile_a_home)
        db_b = _seed_db(profile_b_home)
        hash_b_before = _file_hash(db_b)

        result = maintenance.rebuild_vectors(db_a, hermes_home=profile_a_home, dry_run=False)
        assert result.ok is True

        hash_b_after = _file_hash(db_b)
        assert hash_b_before == hash_b_after


# ---------------------------------------------------------------------------
# No secrets / raw identity in output
# ---------------------------------------------------------------------------

class TestNoSecretsInOutput:
    def test_backfill_dry_run_result_has_no_raw_namespace_or_content(self, tmp_path):
        db_path = _seed_db(tmp_path, with_relationship_candidates=True)
        result = maintenance.backfill_relationships(db_path, hermes_home=tmp_path, apply=False)
        payload = json.dumps(result.to_dict())
        assert "ns_a" not in payload
        assert "Alice" not in payload
        assert "Bob" not in payload
        assert "telegram:111" not in payload

    def test_rebuild_results_have_no_raw_content(self, tmp_path):
        db_path = _seed_db(tmp_path)
        result = maintenance.rebuild_vectors(db_path, hermes_home=tmp_path, dry_run=False)
        payload = json.dumps(result.to_dict())
        assert "A plain fact for maintenance testing." not in payload
        assert "telegram:111" not in payload


# ---------------------------------------------------------------------------
# verify() is a thin alias for diagnostics.run_diagnostics
# ---------------------------------------------------------------------------

class TestVerifyAlias:
    def test_verify_matches_run_diagnostics(self, tmp_path):
        from plugins.memory.holographic import diagnostics

        db_path = _seed_db(tmp_path)
        via_maintenance = maintenance.verify(db_path)
        via_diagnostics = diagnostics.run_diagnostics(db_path)
        assert via_maintenance.to_dict() == via_diagnostics.to_dict()
