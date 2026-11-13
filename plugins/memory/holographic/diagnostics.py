"""Read-only health diagnostics for the Holographic memory plugin.

Original implementation inspired by the checks performed in
``demo-reference/holographic-memory-main/scripts/holographic_memory_health.py``
(integrity_check, fact/entity counts, low-trust/stale scans) but rewritten
against the production schema in ``plugins/memory/holographic/store.py`` and
extended with the checks required by
``docs/plans/demo-reference-plan/implementation-plan.md`` section
"Phase 1 — ... Health diagnostics" (FK checks, FTS parity, orphan rows,
supersession-chain validity, HRR vector/bank validity, status/trust-score
constraints, and backup verification). ``demo-reference/`` is never imported
at runtime — see ``plugins/memory/holographic/README.md``.

Hard safety guarantee: the database connection used here is opened with
SQLite's ``mode=ro`` URI flag, so any accidental write attempt raises
``sqlite3.OperationalError`` instead of silently succeeding — this is a
structural guarantee, not just a convention followed by this module's code.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import backup as backup_module

ALGORITHM_VERSION = "1"
SCHEMA_VERSION = "1"

# ---------------------------------------------------------------------------
# Stable diagnostic codes. Do not rename existing codes — automation and
# tests key off these strings.
# ---------------------------------------------------------------------------
CODE_INTEGRITY_CHECK_FAILED = "INTEGRITY_CHECK_FAILED"
CODE_FOREIGN_KEY_VIOLATION = "FOREIGN_KEY_VIOLATION"
CODE_MISSING_TABLE = "MISSING_TABLE"
CODE_MISSING_COLUMN = "MISSING_COLUMN"
CODE_MISSING_INDEX = "MISSING_INDEX"
CODE_MISSING_TRIGGER = "MISSING_TRIGGER"
CODE_FTS_PARITY_MISMATCH = "FTS_PARITY_MISMATCH"
CODE_ORPHAN_FACT_ENTITY = "ORPHAN_FACT_ENTITY"
CODE_ORPHAN_FACT_SOURCE = "ORPHAN_FACT_SOURCE"
CODE_ORPHAN_RELATIONSHIP = "ORPHAN_RELATIONSHIP"
CODE_INVALID_SUPERSEDED_BY = "INVALID_SUPERSEDED_BY"
CODE_SUPERSESSION_CYCLE = "SUPERSESSION_CYCLE"
CODE_MALFORMED_HRR_VECTOR = "MALFORMED_HRR_VECTOR"
CODE_BANK_DIMENSION_MISMATCH = "BANK_DIMENSION_MISMATCH"
CODE_BANK_COUNT_MISMATCH = "BANK_COUNT_MISMATCH"
CODE_INVALID_FACT_STATUS = "INVALID_FACT_STATUS"
CODE_INVALID_TRUST_SCORE = "INVALID_TRUST_SCORE"
CODE_BACKUP_INVALID = "BACKUP_INVALID"
CODE_DATABASE_NOT_FOUND = "DATABASE_NOT_FOUND"

_EXPECTED_TABLES = frozenset({
    "facts", "entities", "fact_entities", "fact_sources",
    "forgotten_facts", "memory_banks", "facts_fts",
})
_EXPECTED_FACT_COLUMNS = frozenset({
    "fact_id", "content", "category", "tags", "trust_score",
    "retrieval_count", "helpful_count", "created_at", "updated_at",
    "hrr_vector", "status", "superseded_by", "source_type",
    "extraction_method", "confidence", "namespace",
})
_EXPECTED_INDEXES = frozenset({
    "idx_facts_trust", "idx_facts_category", "idx_entities_name",
    "idx_fact_sources_fact_id", "idx_facts_namespace",
    "idx_facts_content_namespace",
})
_EXPECTED_TRIGGERS = frozenset({"facts_ai", "facts_ad", "facts_au"})
_VALID_STATUSES = frozenset({"active", "superseded"})

# Exit-status semantics for CLI use — stable across releases.
EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_CANNOT_RUN = 3


@dataclass
class DiagnosticIssue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "count": self.count}


@dataclass
class DiagnosticReport:
    ok: bool
    issues: list[DiagnosticIssue] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)
    namespaces: list[dict[str, Any]] = field(default_factory=list)
    backup: "dict[str, Any] | None" = None
    algorithm_version: str = ALGORITHM_VERSION
    schema_version: str = SCHEMA_VERSION
    target: str = "live"
    db_path: "str | None" = None
    live_db_path: "str | None" = None

    @property
    def exit_code(self) -> int:
        if any(i.severity == "error" for i in self.issues):
            return EXIT_ERRORS
        if any(i.severity == "warning" for i in self.issues):
            return EXIT_WARNINGS
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "algorithm_version": self.algorithm_version,
            "schema_version": self.schema_version,
            "exit_code": self.exit_code,
            "target": self.target,
            "db_path": self.db_path,
            "live_db_path": self.live_db_path,
            "issues": [i.to_dict() for i in self.issues],
            "counts": self.counts,
            "namespaces": self.namespaces,
            "backup": self.backup,
        }


def _namespace_digest(namespace: str) -> str:
    """Opaque, stable label for a namespace — never the raw chat identity.

    Unscoped (CLI) facts use namespace == "" and are reported as "" (not
    identifying anything); every other namespace is reported only as a
    short digest so standard diagnostic output never discloses a raw
    platform:chat_id string.
    """
    if not namespace:
        return ""
    return hashlib.sha1(namespace.encode("utf-8")).hexdigest()[:12]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,),
    ).fetchone()
    return row is not None


def _check_tables_columns_indexes_triggers(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    existing_tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    }
    for table in sorted(_EXPECTED_TABLES):
        if table not in existing_tables:
            issues.append(DiagnosticIssue(CODE_MISSING_TABLE, "error", f"missing table: {table}"))

    if "facts" in existing_tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
        for col in sorted(_EXPECTED_FACT_COLUMNS):
            if col not in columns:
                issues.append(DiagnosticIssue(CODE_MISSING_COLUMN, "error", f"facts missing column: {col}"))

    existing_indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    for idx in sorted(_EXPECTED_INDEXES):
        if idx not in existing_indexes:
            issues.append(DiagnosticIssue(CODE_MISSING_INDEX, "warning", f"missing index: {idx}"))

    existing_triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()}
    for trig in sorted(_EXPECTED_TRIGGERS):
        if trig not in existing_triggers:
            issues.append(DiagnosticIssue(CODE_MISSING_TRIGGER, "error", f"missing FTS trigger: {trig}"))


def _check_integrity_and_fk(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result and result[0] != "ok":
        issues.append(DiagnosticIssue(CODE_INTEGRITY_CHECK_FAILED, "error", str(result[0])))

    try:
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        fk_rows = []
        issues.append(DiagnosticIssue(CODE_FOREIGN_KEY_VIOLATION, "error", f"foreign_key_check failed: {exc}"))
    if fk_rows:
        issues.append(DiagnosticIssue(
            CODE_FOREIGN_KEY_VIOLATION, "error",
            f"{len(fk_rows)} foreign key violation(s)", count=len(fk_rows),
        ))


def _check_fts_parity(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    """Compare facts' row IDs against the FTS5 index's own shadow table.

    ``SELECT ... FROM facts_fts`` with no MATCH clause on an external-content
    (``content=facts``) FTS5 table is a plain join against the content
    table itself and will report every content row regardless of whether
    it is actually indexed — it is NOT a reliable parity signal. The
    ``<table>_docsize`` shadow table that FTS5 maintains internally,
    however, has exactly one row per row that is actually present in the
    full-text index, so comparing against *that* catches a desynced index
    (e.g. a trigger that silently failed to fire).
    """
    if not _table_exists(conn, "facts") or not _table_exists(conn, "facts_fts"):
        return
    try:
        facts_ids = {int(r[0]) for r in conn.execute("SELECT fact_id FROM facts").fetchall()}
        indexed_ids = {int(r[0]) for r in conn.execute("SELECT rowid FROM facts_fts_docsize").fetchall()}
    except sqlite3.DatabaseError as exc:
        issues.append(DiagnosticIssue(CODE_FTS_PARITY_MISMATCH, "error", f"facts_fts shadow tables unreadable: {exc}"))
        return
    if facts_ids != indexed_ids:
        missing = len(facts_ids - indexed_ids)
        extra = len(indexed_ids - facts_ids)
        issues.append(DiagnosticIssue(
            CODE_FTS_PARITY_MISMATCH, "error",
            f"facts_fts index out of sync with facts: {missing} unindexed fact(s), {extra} stale index entr(y/ies)",
        ))


def _check_orphans(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    if _table_exists(conn, "fact_entities") and _table_exists(conn, "facts") and _table_exists(conn, "entities"):
        orphan_fe = conn.execute(
            """
            SELECT COUNT(*) FROM fact_entities fe
            WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.fact_id = fe.fact_id)
               OR NOT EXISTS (SELECT 1 FROM entities e WHERE e.entity_id = fe.entity_id)
            """
        ).fetchone()[0]
        if orphan_fe:
            issues.append(DiagnosticIssue(CODE_ORPHAN_FACT_ENTITY, "error", f"{orphan_fe} orphaned fact_entities row(s)", count=orphan_fe))

    if _table_exists(conn, "fact_sources") and _table_exists(conn, "facts"):
        orphan_fs = conn.execute(
            """
            SELECT COUNT(*) FROM fact_sources fs
            WHERE NOT EXISTS (SELECT 1 FROM facts f WHERE f.fact_id = fs.fact_id)
            """
        ).fetchone()[0]
        if orphan_fs:
            issues.append(DiagnosticIssue(CODE_ORPHAN_FACT_SOURCE, "error", f"{orphan_fs} orphaned fact_sources row(s)", count=orphan_fs))


def _check_relationship_orphans(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    """Read-only: detect `entity_relationships` rows whose source or target
    entity no longer exists (checklist section 3's "Detect orphaned
    relationships" — left unimplemented until Phase 5 added the
    `entity_relationships` table/service this check depends on).

    `entity_relationships` is intentionally NOT in `_EXPECTED_TABLES` — it is
    additive (Phase 5), not part of the Phase 1 schema contract, so this
    check is a no-op (not a MISSING_TABLE issue) on any database that
    doesn't have it yet. Reports only; never mutates or repairs a row,
    matching every other check in this read-only module.
    """
    if not _table_exists(conn, "entity_relationships") or not _table_exists(conn, "entities"):
        return
    orphan = conn.execute(
        """
        SELECT COUNT(*) FROM entity_relationships r
        WHERE NOT EXISTS (SELECT 1 FROM entities e WHERE e.entity_id = r.source_entity_id)
           OR NOT EXISTS (SELECT 1 FROM entities e WHERE e.entity_id = r.target_entity_id)
        """
    ).fetchone()[0]
    if orphan:
        issues.append(DiagnosticIssue(
            CODE_ORPHAN_RELATIONSHIP, "error", f"{orphan} orphaned entity_relationships row(s)", count=orphan,
        ))


def _facts_has_columns(conn: sqlite3.Connection, *columns: str) -> bool:
    if not _table_exists(conn, "facts"):
        return False
    existing = {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
    return all(c in existing for c in columns)


def _check_supersession_chain(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    if not _facts_has_columns(conn, "fact_id", "status", "superseded_by"):
        return
    rows = conn.execute("SELECT fact_id, status, superseded_by FROM facts").fetchall()
    by_id = {int(r[0]): (r[1], r[2]) for r in rows}

    invalid = 0
    for fact_id, (status, superseded_by) in by_id.items():
        if superseded_by is None:
            continue
        if superseded_by not in by_id:
            invalid += 1
        elif superseded_by == fact_id:
            invalid += 1
    if invalid:
        issues.append(DiagnosticIssue(CODE_INVALID_SUPERSEDED_BY, "error", f"{invalid} invalid superseded_by reference(s)", count=invalid))

    visited_global: set[int] = set()
    cycles = 0
    for fact_id in by_id:
        if fact_id in visited_global:
            continue
        path: list[int] = []
        node = fact_id
        seen_this_walk: set[int] = set()
        while node is not None and node in by_id:
            if node in seen_this_walk:
                cycles += 1
                break
            seen_this_walk.add(node)
            path.append(node)
            node = by_id[node][1]
        visited_global.update(path)
    if cycles:
        issues.append(DiagnosticIssue(CODE_SUPERSESSION_CYCLE, "error", f"{cycles} supersession cycle(s) detected", count=cycles))


def _check_hrr_vectors_and_banks(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    if not _facts_has_columns(conn, "fact_id", "hrr_vector", "category", "status"):
        return
    rows = conn.execute("SELECT fact_id, hrr_vector FROM facts WHERE hrr_vector IS NOT NULL").fetchall()
    lengths: dict[int, int] = {}
    malformed = 0
    for fact_id, blob in rows:
        if blob is None:
            continue
        n = len(blob)
        if n == 0 or n % 8 != 0:
            malformed += 1
            continue
        lengths[n] = lengths.get(n, 0) + 1
    if lengths:
        expected_len = max(lengths, key=lambda k: lengths[k])
        off_dimension = sum(count for length, count in lengths.items() if length != expected_len)
        malformed += off_dimension
    if malformed:
        issues.append(DiagnosticIssue(CODE_MALFORMED_HRR_VECTOR, "error", f"{malformed} malformed/wrong-dimension HRR vector(s)", count=malformed))

    if not _table_exists(conn, "memory_banks"):
        return
    banks = conn.execute("SELECT bank_name, vector, dim, fact_count FROM memory_banks").fetchall()
    dim_mismatches = 0
    count_mismatches = 0
    for bank_name, vector, dim, fact_count in banks:
        if vector is None or dim is None or len(vector) != int(dim) * 8:
            dim_mismatches += 1
            continue
        # Bank name formats (store.bank_name()):
        #   "cat:<category>"              unscoped/CLI bank (namespace == "")
        #   "cat:ns:<digest>:<category>"  namespace-specific bank — the
        #     namespace is folded into a one-way digest and cannot be
        #     recovered here, so its exact fact_count can't be independently
        #     re-derived from a raw namespace string. Skip the parity check
        #     for those rather than reporting a false mismatch.
        if not isinstance(bank_name, str) or ":" not in bank_name:
            continue
        parts = bank_name.split(":")
        if len(parts) >= 2 and parts[0] == "cat" and parts[1] == "ns":
            continue
        category = bank_name.split(":", 1)[1]
        actual = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE category = ? AND namespace = '' "
            "AND hrr_vector IS NOT NULL AND status != 'superseded'",
            (category,),
        ).fetchone()[0]
        if actual != fact_count:
            count_mismatches += 1
    if dim_mismatches:
        issues.append(DiagnosticIssue(CODE_BANK_DIMENSION_MISMATCH, "error", f"{dim_mismatches} memory_banks row(s) with invalid dimension", count=dim_mismatches))
    if count_mismatches:
        issues.append(DiagnosticIssue(CODE_BANK_COUNT_MISMATCH, "warning", f"{count_mismatches} memory_banks row(s) whose fact_count disagrees with current facts", count=count_mismatches))


def _check_status_and_trust(conn: sqlite3.Connection, issues: list[DiagnosticIssue]) -> None:
    if not _facts_has_columns(conn, "status", "trust_score"):
        return
    rows = conn.execute("SELECT status, trust_score FROM facts").fetchall()
    bad_status = sum(1 for status, _ in rows if status not in _VALID_STATUSES)
    bad_trust = sum(1 for _, trust in rows if trust is None or trust < 0.0 or trust > 1.0)
    if bad_status:
        issues.append(DiagnosticIssue(CODE_INVALID_FACT_STATUS, "error", f"{bad_status} fact(s) with an invalid status value", count=bad_status))
    if bad_trust:
        issues.append(DiagnosticIssue(CODE_INVALID_TRUST_SCORE, "error", f"{bad_trust} fact(s) with trust_score outside [0, 1]", count=bad_trust))


def _namespace_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-namespace counts only — never raw fact content, never a raw
    namespace string (see ``_namespace_digest``)."""
    rows = conn.execute(
        """
        SELECT namespace,
               COUNT(*) AS total,
               SUM(CASE WHEN status != 'superseded' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN status = 'superseded' THEN 1 ELSE 0 END) AS superseded
        FROM facts
        GROUP BY namespace
        ORDER BY namespace
        """
    ).fetchall()
    return [
        {
            "namespace": _namespace_digest(ns or ""),
            "unscoped": not bool(ns),
            "total": int(total or 0),
            "active": int(active or 0),
            "superseded": int(superseded or 0),
        }
        for ns, total, active, superseded in rows
    ]


def run_diagnostics(
    db_path: "str | Path",
    *,
    backup_path: "str | Path | None" = None,
) -> DiagnosticReport:
    """Run every read-only health check against ``db_path`` — or, when
    ``backup_path`` is given, against the backup file itself.

    When ``backup_path`` is set, this diagnoses **that backup file**, not
    ``db_path``: the backup's own checksum/integrity is verified first (via
    ``backup.verify_backup``), and if that passes, every check below (and
    every count) runs against the backup, never mixing in the live DB's
    state. ``db_path`` is only used to report ``live_db_path`` for context.
    If verification fails, diagnostics stop there — the (unverified) backup
    file is never opened for further checks, so a corrupt backup can't
    produce a misleadingly clean report.

    Opens the diagnosed database with SQLite's ``mode=ro`` URI flag — the
    single strongest guarantee available that this function cannot write to
    the database, independent of any bug in the checks below.
    """
    live_db_path = Path(db_path)
    target = "live"
    diagnose_path = live_db_path
    backup_result = None

    if backup_path is not None:
        target = "backup"
        diagnose_path = Path(backup_path)
        verify = backup_module.verify_backup(str(diagnose_path))
        backup_result = {"ok": verify.ok, "reason": verify.reason}
        if not verify.ok:
            return DiagnosticReport(
                ok=False,
                issues=[DiagnosticIssue(CODE_BACKUP_INVALID, "error", f"backup invalid: {verify.reason}")],
                backup=backup_result,
                target=target,
                db_path=str(diagnose_path),
                live_db_path=str(live_db_path),
            )

    if not diagnose_path.exists():
        return DiagnosticReport(
            ok=False,
            issues=[DiagnosticIssue(CODE_DATABASE_NOT_FOUND, "error", f"database not found: {diagnose_path}")],
            backup=backup_result,
            target=target,
            db_path=str(diagnose_path),
            live_db_path=str(live_db_path) if target == "backup" else None,
        )

    uri = f"file:{diagnose_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        issues: list[DiagnosticIssue] = []
        _check_integrity_and_fk(conn, issues)
        _check_tables_columns_indexes_triggers(conn, issues)
        _check_fts_parity(conn, issues)
        _check_orphans(conn, issues)
        _check_relationship_orphans(conn, issues)
        _check_supersession_chain(conn, issues)
        _check_hrr_vectors_and_banks(conn, issues)
        _check_status_and_trust(conn, issues)

        counts = {}
        if _table_exists(conn, "facts"):
            counts["facts"] = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
            if _facts_has_columns(conn, "status"):
                counts["active"] = int(conn.execute("SELECT COUNT(*) FROM facts WHERE status != 'superseded'").fetchone()[0])
                counts["superseded"] = int(conn.execute("SELECT COUNT(*) FROM facts WHERE status = 'superseded'").fetchone()[0])
        if _table_exists(conn, "entities"):
            counts["entities"] = int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])
        if _table_exists(conn, "forgotten_facts"):
            counts["tombstones"] = int(conn.execute("SELECT COUNT(*) FROM forgotten_facts").fetchone()[0])
        if _table_exists(conn, "memory_banks"):
            counts["memory_banks"] = int(conn.execute("SELECT COUNT(*) FROM memory_banks").fetchone()[0])

        namespaces = _namespace_summary(conn) if _facts_has_columns(conn, "namespace", "status") else []
    finally:
        conn.close()

    ok = not any(i.severity == "error" for i in issues)
    return DiagnosticReport(
        ok=ok,
        issues=issues,
        counts=counts,
        namespaces=namespaces,
        backup=backup_result,
        target=target,
        db_path=str(diagnose_path),
        live_db_path=str(live_db_path) if target == "backup" else None,
    )
