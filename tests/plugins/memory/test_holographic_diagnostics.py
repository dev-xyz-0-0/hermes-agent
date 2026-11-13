"""Phase 1 — health diagnostics tests.

See docs/plans/demo-reference-plan/checklist.md section 3 ("Diagnostics
tests") and implementation-plan.md section 9.7.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from plugins.memory.holographic import backup as backup_module
from plugins.memory.holographic import diagnostics
from plugins.memory.holographic.store import MemoryStore


def _file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestHealthyDatabase:
    def test_healthy_db_reports_success(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("A healthy fact.", category="general")
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        assert report.ok is True
        assert report.exit_code == diagnostics.EXIT_OK
        assert report.issues == []

    def test_json_output_is_stable_and_machine_readable(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("A healthy fact.", category="general")
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        serialized = json.dumps(report.to_dict())
        parsed = json.loads(serialized)
        for key in ("ok", "algorithm_version", "schema_version", "exit_code", "issues", "counts", "namespaces"):
            assert key in parsed


class TestMutationSafety:
    def test_diagnostics_do_not_mutate_the_db(self, tmp_path, current_schema_mixed_status_db):
        db_path, _ids = current_schema_mixed_status_db
        before_hash = _file_hash(db_path)
        before_change_count = sqlite3.connect(str(db_path)).execute("PRAGMA data_version").fetchone()[0]

        diagnostics.run_diagnostics(db_path)

        after_hash = _file_hash(db_path)
        after_change_count = sqlite3.connect(str(db_path)).execute("PRAGMA data_version").fetchone()[0]
        assert before_hash == after_hash
        assert before_change_count == after_change_count

    def test_diagnostics_opens_connection_read_only(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact", category="general")
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        assert report.ok is True  # no exception raised just by running


class TestMissingSchemaDetection:
    def test_missing_table_reports_expected_code(self, tmp_path):
        db_path = tmp_path / "broken.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE facts (fact_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        report = diagnostics.run_diagnostics(db_path)
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_MISSING_TABLE in codes
        assert report.ok is False

    def test_missing_index_reports_expected_code(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact", category="general")
        store._conn.execute("DROP INDEX IF EXISTS idx_facts_trust")
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_MISSING_INDEX in codes

    def test_missing_trigger_reports_expected_code(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact", category="general")
        store._conn.execute("DROP TRIGGER IF EXISTS facts_ai")
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_MISSING_TRIGGER in codes
        assert report.exit_code == diagnostics.EXIT_ERRORS


class TestFtsParity:
    def test_fts_parity_mismatch_reports_expected_code(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact one", category="general")
        store.add_fact("fact two", category="general")
        # Desync the external-content FTS index from facts without going
        # through the trigger-maintained path, using the same special
        # 'delete' command the facts_ad trigger itself uses.
        store._conn.execute(
            "INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', 1, 'fact one', '')"
        )
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_FTS_PARITY_MISMATCH in codes


class TestOrphanDetection:
    def test_orphan_fact_entities_reports_expected_code(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("Alice Smith works here.", category="general")
        store.close()

        # Insert the orphan row via a separate connection with FK
        # enforcement off, since MemoryStore's own connection enforces FKs.
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (999, 1)")
        conn.commit()
        conn.close()

        report = diagnostics.run_diagnostics(db_path)
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_ORPHAN_FACT_ENTITY in codes

    def test_orphan_fact_sources_reports_expected_code(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        store.add_fact("fact", category="general")
        store.close()

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO fact_sources (fact_id, via, platform) VALUES (999, 'x', 'y')")
        conn.commit()
        conn.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_ORPHAN_FACT_SOURCE in codes

    def test_orphan_relationship_reports_expected_code_without_mutating(self, tmp_path):
        """Phase 5 — checklist section 3's 'Detect orphaned relationships'
        and section 8's 'Health diagnostics detect orphan edges without
        repairing them'."""
        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        alice = store._resolve_entity("Alice")
        store._conn.commit()
        store.close()

        # Insert the orphan row via a separate connection with FK
        # enforcement off, matching the fact_entities/fact_sources orphan
        # tests above — MemoryStore's own connection enforces FKs.
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO entity_relationships (source_entity_id, target_entity_id, relationship_type, namespace) "
            "VALUES (?, 999999, 'manages', 'ns')",
            (alice,),
        )
        conn.commit()
        conn.close()

        before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        report = diagnostics.run_diagnostics(db_path)
        after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()

        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_ORPHAN_RELATIONSHIP in codes
        assert report.ok is False
        assert before_hash == after_hash  # read-only — never repairs the orphan

    def test_no_orphan_relationships_on_healthy_db(self, tmp_path):
        db_path = tmp_path / "db.sqlite"
        store = MemoryStore(db_path=db_path, hrr_dim=32)
        alice = store._resolve_entity("Alice")
        bob = store._resolve_entity("Bob")
        store._conn.commit()
        store.upsert_relationship(alice, bob, "manages", namespace="ns")
        store.close()

        report = diagnostics.run_diagnostics(db_path)
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_ORPHAN_RELATIONSHIP not in codes


class TestSupersessionValidity:
    def test_invalid_superseded_by_reference_detected(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        fact_id = store.add_fact("fact", category="general")
        store._conn.execute(
            "UPDATE facts SET status = 'superseded', superseded_by = 99999 WHERE fact_id = ?", (fact_id,)
        )
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_INVALID_SUPERSEDED_BY in codes

    def test_supersession_cycle_detected(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        a = store.add_fact("fact a", category="general")
        b = store.add_fact("fact b", category="general")
        store._conn.execute(
            "UPDATE facts SET status='superseded', superseded_by = ? WHERE fact_id = ?", (b, a)
        )
        store._conn.execute(
            "UPDATE facts SET status='superseded', superseded_by = ? WHERE fact_id = ?", (a, b)
        )
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_SUPERSESSION_CYCLE in codes

    def test_valid_supersession_chain_is_clean(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        old_id = store.add_fact("old value", category="general")
        store.supersede_fact(old_id, "new value", category="general")
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_INVALID_SUPERSEDED_BY not in codes
        assert diagnostics.CODE_SUPERSESSION_CYCLE not in codes


class TestHrrAndBankValidity:
    def test_malformed_hrr_vector_detected(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        fact_id = store.add_fact("fact with a vector", category="general")
        store._conn.execute("UPDATE facts SET hrr_vector = ? WHERE fact_id = ?", (b"\x00\x01\x02", fact_id))
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_MALFORMED_HRR_VECTOR in codes

    def test_bank_count_mismatch_detected(self, tmp_path, numpy_enabled):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact one", category="general")
        store._conn.execute("UPDATE memory_banks SET fact_count = fact_count + 5 WHERE bank_name = 'cat:general'")
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_BANK_COUNT_MISMATCH in codes


class TestStatusAndTrustValidity:
    def test_invalid_status_detected(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        fact_id = store.add_fact("fact", category="general")
        store._conn.execute("UPDATE facts SET status = 'bogus' WHERE fact_id = ?", (fact_id,))
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_INVALID_FACT_STATUS in codes

    def test_invalid_trust_score_detected(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        fact_id = store.add_fact("fact", category="general")
        store._conn.execute("UPDATE facts SET trust_score = 5.0 WHERE fact_id = ?", (fact_id,))
        store._conn.commit()
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_INVALID_TRUST_SCORE in codes


class TestBackupVerification:
    def test_valid_backup_reports_ok(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact", category="general")
        store.close()

        info = backup_module.create_backup(str(tmp_path / "db.sqlite"), str(tmp_path / "backups"))
        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite", backup_path=info.path)
        assert report.backup == {"ok": True, "reason": ""}

    def test_corrupt_backup_reports_expected_code(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact", category="general")
        store.close()

        info = backup_module.create_backup(str(tmp_path / "db.sqlite"), str(tmp_path / "backups"))
        with open(info.path, "r+b") as f:
            f.seek(100)
            f.write(b"\xff" * 16)

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite", backup_path=info.path)
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_BACKUP_INVALID in codes
        assert report.backup["ok"] is False

    def test_backup_path_diagnostics_use_backup_db_not_live(self, tmp_path):
        live = tmp_path / "live.sqlite"
        store = MemoryStore(db_path=live, hrr_dim=32)
        store.add_fact("live only fact", category="general", namespace="")
        store.close()

        # Stale backup snapshot with its own bank-count mismatch that does
        # not exist on live.
        stale = tmp_path / "stale.sqlite"
        s2 = MemoryStore(db_path=stale, hrr_dim=32)
        s2.add_fact("stale fact", category="project", namespace="")
        s2._conn.execute("UPDATE memory_banks SET fact_count = fact_count + 5")
        s2._conn.commit()
        s2.close()

        info = backup_module.create_backup(str(stale), str(tmp_path / "backups"))

        report = diagnostics.run_diagnostics(live, backup_path=info.path)

        assert report.backup == {"ok": True, "reason": ""}
        # Counts/issues must reflect the backup (stale), not live.
        assert report.counts["facts"] == 1
        codes = {i.code for i in report.issues}
        assert diagnostics.CODE_BANK_COUNT_MISMATCH in codes

        # Live itself is clean when diagnosed alone.
        live_report = diagnostics.run_diagnostics(live)
        assert diagnostics.CODE_BANK_COUNT_MISMATCH not in {i.code for i in live_report.issues}

    def test_backup_path_live_unaffected_by_backup_diagnosis(self, tmp_path):
        """A dirty live DB stays dirty, and a clean backup can report clean,
        independently of each other — proves counts/issues are not mixed."""
        live = tmp_path / "live.sqlite"
        store = MemoryStore(db_path=live, hrr_dim=32)
        store.add_fact("live fact", category="general", namespace="")
        store._conn.execute("UPDATE memory_banks SET fact_count = fact_count + 5")
        store._conn.commit()
        store.close()

        clean_backup_source = tmp_path / "clean.sqlite"
        s2 = MemoryStore(db_path=clean_backup_source, hrr_dim=32)
        s2.add_fact("clean fact", category="general", namespace="")
        s2.close()
        info = backup_module.create_backup(str(clean_backup_source), str(tmp_path / "backups2"))

        backup_report = diagnostics.run_diagnostics(live, backup_path=info.path)
        assert diagnostics.CODE_BANK_COUNT_MISMATCH not in {i.code for i in backup_report.issues}

        live_report = diagnostics.run_diagnostics(live)
        assert diagnostics.CODE_BANK_COUNT_MISMATCH in {i.code for i in live_report.issues}


class TestNamespaceRedaction:
    def test_standard_output_never_shows_raw_namespace_or_content(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("A secret project called Nightjar.", category="project", namespace="telegram:123456789")
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        dumped = json.dumps(report.to_dict())
        assert "telegram:123456789" not in dumped
        assert "Nightjar" not in dumped
        assert len(report.namespaces) == 1
        assert report.namespaces[0]["namespace"] != "telegram:123456789"

    def test_unscoped_namespace_reported_as_empty(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        store.add_fact("fact", category="general")
        store.close()

        report = diagnostics.run_diagnostics(tmp_path / "db.sqlite")
        assert report.namespaces[0]["namespace"] == ""
        assert report.namespaces[0]["unscoped"] is True


class TestDatabaseNotFound:
    def test_missing_database_reports_safe_error(self, tmp_path):
        report = diagnostics.run_diagnostics(tmp_path / "missing.db")
        assert report.ok is False
        assert any(i.code == diagnostics.CODE_DATABASE_NOT_FOUND for i in report.issues)
