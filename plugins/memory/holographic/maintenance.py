"""Operator-facing maintenance service for the Holographic memory plugin.

Phase 6 of docs/plans/demo-reference-plan/implementation-plan.md. This
module is the thing `hermes memory rebuild-vectors`, `rebuild-banks`,
`backfill-relationships`, and `verify` call into — see
``hermes_cli/memory_setup.py`` for the CLI wrapper that resolves the active
profile's database path and prints/exits.

Nothing here is reimplemented from scratch: every write path reuses the
already-idempotent, already namespace-safe primitives added in earlier
phases —

- ``store.MemoryStore.rebuild_all_vectors`` / ``rebuild_bank`` (Phase 2)
- ``relationships.upsert_relationship`` (Phase 5, upsert-safe)
- ``backup.create_backup`` / ``verify_backup`` (pre-existing)
- ``diagnostics.run_diagnostics`` (Phase 1)

This module's own job is narrow: resolve-and-lock-and-backup-and-call, with
dry-run support and a stable JSON/exit-code contract. It is NOT wired into
``get_tool_schemas()``/``handle_tool_call()`` and NOT reachable from any
gateway slash command — see ``hermes_cli/main.py`` for the only caller
(the ``hermes memory`` argparse subcommands) and
``tests/hermes_cli/test_memory_maintenance_cli.py`` for the check that
COMMAND_REGISTRY/gateway dispatch never picks these up.

Locking: there is no pre-existing cross-process maintenance lock for this
plugin, so this module adds one — a plain ``fcntl.flock(LOCK_EX | LOCK_NB)``
on a sidecar ``<db>.maintenance.lock`` file next to the database, following
the exact pattern already used by ``tools/memory_tool.py``'s
``MemoryStore._file_lock`` (a separate lock file, never the data file
itself, so the data file can still be replaced atomically by unrelated
code). Non-blocking: a second concurrent invocation against the SAME
database fails fast with :class:`MaintenanceLockError` (exit code
``EXIT_LOCKED``) rather than hanging — "fail cleanly" per
implementation-plan.md Phase 6's locking requirement. Two profiles pointed
at two different databases never contend for the same lock file, since the
lock path is derived from the resolved db_path.

Backup-before-write: every ``--apply``/write path calls
``backup.create_backup`` and immediately ``backup.verify_backup`` on the
result, and aborts (no writes, nonzero exit) if verification fails — this
happens *before* the lock is even needed for the write itself conceptually,
but is performed while holding the lock so no other maintenance invocation
can mutate the database between backup and the subsequent write.

Extraction heuristic for ``backfill_relationships`` (documented here, not
guessed by a reader of the CLI): it does **not** re-run the capture-time
entity regexes. It reuses entities *already* resolved and linked by
``store.MemoryStore._resolve_entity``/``_link_fact_entity`` at fact-write
time (the ``fact_entities`` join table) — for every active fact that has
two or more distinct linked entities, every unordered pair of those entities
is a candidate "co_occurs_with" relationship, scoped to that fact's
namespace, with a fixed low confidence (0.3) and
``extraction_method="backfill_cooccurrence"``. This is deterministic, needs
no new regex/heuristic surface, and is naturally idempotent: pairs are
canonicalized as ``(min(entity_id), max(entity_id))`` so re-running the
backfill (e.g. after an interruption) upserts the exact same edge rather
than creating a mirrored duplicate in the other direction.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import backup as backup_module
from . import relationships as relationships_module
from .store import MemoryStore

ALGORITHM_VERSION = "1"
SCHEMA_VERSION = "1"

# Stable exit-code contract for automation (documented in
# docs/plans/demo-reference-plan/checklist.md section 9 and the Phase 6 CLI
# tests). Distinct from diagnostics.py's EXIT_OK/EXIT_WARNINGS/EXIT_ERRORS/
# EXIT_CANNOT_RUN only in adding EXIT_LOCKED for the one failure mode that
# diagnostics (read-only, no locking) never needed.
EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_CANNOT_RUN = 3
EXIT_LOCKED = 4

_RELATIONSHIP_TYPE = "co_occurs_with"
_RELATIONSHIP_CONFIDENCE = 0.3
_RELATIONSHIP_STRENGTH = 0.5
_EXTRACTION_METHOD = "backfill_cooccurrence"


class MaintenanceError(ValueError):
    """Stable, safe error for malformed/unsafe maintenance input."""


class MaintenanceLockError(MaintenanceError):
    """Raised when another maintenance invocation already holds the lock
    for this database."""


@dataclass
class MaintenanceResult:
    ok: bool
    dry_run: bool
    operation: str
    db_path: str
    counts: dict[str, Any] = field(default_factory=dict)
    backup: "dict[str, Any] | None" = None
    issues: list[str] = field(default_factory=list)
    algorithm_version: str = ALGORITHM_VERSION
    schema_version: str = SCHEMA_VERSION

    @property
    def exit_code(self) -> int:
        if not self.ok:
            return EXIT_ERRORS
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "operation": self.operation,
            "db_path": self.db_path,
            "algorithm_version": self.algorithm_version,
            "schema_version": self.schema_version,
            "exit_code": self.exit_code,
            "counts": self.counts,
            "backup": self.backup,
            "issues": self.issues,
        }


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def _lock_path_for(db_path: "str | Path") -> Path:
    return Path(str(db_path) + ".maintenance.lock")


@contextlib.contextmanager
def maintenance_lock(db_path: "str | Path", *, retries: int = 0, retry_delay: float = 0.2):
    """Exclusive, non-blocking, cross-process lock scoped to ``db_path``.

    Raises :class:`MaintenanceLockError` immediately (after ``retries``
    short retries, default zero = fail fast) if another process already
    holds the lock for the SAME database file — a different database (e.g.
    a different profile's HERMES_HOME) has its own lock path and never
    contends with this one.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX-only codebase assumption
        fcntl = None  # type: ignore[assignment]

    lock_path = _lock_path_for(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+")
    try:
        if fcntl is not None:
            attempt = 0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    attempt += 1
                    if attempt > retries:
                        raise MaintenanceLockError(
                            f"another maintenance operation holds the lock for {db_path}"
                        ) from exc
                    time.sleep(retry_delay)
        try:
            fd.seek(0)
            fd.truncate()
            fd.write(f"{os.getpid()} {time.time()}\n")
            fd.flush()
        except OSError:
            pass
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        fd.close()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _require_db(db_path: "str | Path") -> Path:
    db_path = Path(db_path)
    if not db_path.exists():
        raise MaintenanceError(f"database not found: {db_path}")
    return db_path


def _readonly_connection(db_path: "str | Path") -> sqlite3.Connection:
    """Open ``db_path`` with SQLite's ``mode=ro`` URI flag — used for every
    dry-run/count-only path so a dry-run is structurally incapable of
    writing, independent of any bug in the counting logic (same guarantee
    diagnostics.py relies on)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _backup_and_verify(db_path: Path, hermes_home: "str | Path") -> dict[str, Any]:
    backup_dir = str(Path(hermes_home) / "backups" / "holographic" / "maintenance")
    info = backup_module.create_backup(str(db_path), backup_dir)
    verify = backup_module.verify_backup(info.path)
    return {
        "ok": verify.ok,
        "reason": verify.reason,
        "path": info.path,
        "fact_count": info.fact_count,
    }


# ---------------------------------------------------------------------------
# rebuild-vectors
# ---------------------------------------------------------------------------

def rebuild_vectors(
    db_path: "str | Path",
    *,
    hermes_home: "str | Path",
    dry_run: bool = True,
    hrr_dim: int = 1024,
) -> MaintenanceResult:
    """Recompute HRR vectors for every active fact (dimension-consistent).

    Wraps ``MemoryStore.rebuild_all_vectors(namespace=None)`` (Phase 2),
    which is already idempotent — rebuilding twice produces the same
    vectors both times, so interrupting this operation and re-running it is
    always safe to restart.
    """
    db_path = _require_db(db_path)

    if dry_run:
        conn = _readonly_connection(db_path)
        try:
            planned = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
        finally:
            conn.close()
        return MaintenanceResult(
            ok=True, dry_run=True, operation="rebuild-vectors", db_path=str(db_path),
            counts={"facts_planned": planned},
        )

    with maintenance_lock(db_path):
        backup_info = _backup_and_verify(db_path, hermes_home)
        if not backup_info["ok"]:
            return MaintenanceResult(
                ok=False, dry_run=False, operation="rebuild-vectors", db_path=str(db_path),
                backup=backup_info, issues=[f"backup verification failed: {backup_info['reason']}"],
            )

        store = MemoryStore(db_path=db_path, hrr_dim=hrr_dim)
        try:
            processed = store.rebuild_all_vectors(namespace=None)
        finally:
            store.close()

        return MaintenanceResult(
            ok=True, dry_run=False, operation="rebuild-vectors", db_path=str(db_path),
            counts={"facts_processed": processed}, backup=backup_info,
        )


# ---------------------------------------------------------------------------
# rebuild-banks
# ---------------------------------------------------------------------------

def _active_namespace_category_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT namespace, category FROM facts WHERE status != 'superseded'"
    ).fetchall()
    return sorted({(row["namespace"] or "", row["category"] or "general") for row in rows})


def rebuild_banks(
    db_path: "str | Path",
    *,
    hermes_home: "str | Path",
    dry_run: bool = True,
    hrr_dim: int = 1024,
) -> MaintenanceResult:
    """Rebuild every (namespace, category) HRR memory bank.

    Wraps ``MemoryStore.rebuild_bank`` (Phase 2) once per affected pair.
    Already idempotent per-pair (a full rebuild from current active facts,
    not an incremental update), so repeated/interrupted runs converge to
    the same end state.
    """
    db_path = _require_db(db_path)

    if dry_run:
        conn = _readonly_connection(db_path)
        try:
            pairs = _active_namespace_category_pairs(conn)
        finally:
            conn.close()
        return MaintenanceResult(
            ok=True, dry_run=True, operation="rebuild-banks", db_path=str(db_path),
            counts={"pairs_planned": len(pairs)},
        )

    with maintenance_lock(db_path):
        backup_info = _backup_and_verify(db_path, hermes_home)
        if not backup_info["ok"]:
            return MaintenanceResult(
                ok=False, dry_run=False, operation="rebuild-banks", db_path=str(db_path),
                backup=backup_info, issues=[f"backup verification failed: {backup_info['reason']}"],
            )

        store = MemoryStore(db_path=db_path, hrr_dim=hrr_dim)
        try:
            conn = store._conn
            pairs = _active_namespace_category_pairs(conn)
            for namespace, category in pairs:
                store.rebuild_bank(category, namespace)
        finally:
            store.close()

        return MaintenanceResult(
            ok=True, dry_run=False, operation="rebuild-banks", db_path=str(db_path),
            counts={"pairs_rebuilt": len(pairs)}, backup=backup_info,
        )


# ---------------------------------------------------------------------------
# backfill-relationships
# ---------------------------------------------------------------------------

def _candidate_cooccurrence_pairs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Deterministic candidate extraction (see module docstring).

    Returns one dict per (fact, entity_a, entity_b) candidate, with
    ``entity_a < entity_b`` already canonicalized. Only active facts
    contribute — superseded/forgotten facts never do (forgotten facts are
    hard-deleted from `facts`, so they can't appear here at all; superseded
    ones are filtered explicitly).
    """
    rows = conn.execute(
        """
        SELECT f.fact_id, f.namespace, fe.entity_id
        FROM facts f
        JOIN fact_entities fe ON fe.fact_id = f.fact_id
        WHERE f.status != 'superseded'
        ORDER BY f.fact_id, fe.entity_id
        """
    ).fetchall()

    by_fact: dict[int, dict[str, Any]] = {}
    for row in rows:
        fact_id = int(row["fact_id"])
        entry = by_fact.setdefault(fact_id, {"namespace": row["namespace"] or "", "entity_ids": []})
        entry["entity_ids"].append(int(row["entity_id"]))

    candidates: list[dict[str, Any]] = []
    for fact_id, entry in by_fact.items():
        entity_ids = sorted(set(entry["entity_ids"]))
        if len(entity_ids) < 2:
            continue
        for i in range(len(entity_ids)):
            for j in range(i + 1, len(entity_ids)):
                candidates.append({
                    "fact_id": fact_id,
                    "namespace": entry["namespace"],
                    "source_entity_id": entity_ids[i],
                    "target_entity_id": entity_ids[j],
                })
    return candidates


def backfill_relationships(
    db_path: "str | Path",
    *,
    hermes_home: "str | Path",
    apply: bool = False,
    hrr_dim: int = 1024,
) -> MaintenanceResult:
    """Scan existing facts for deterministic co-occurrence relationship
    candidates and upsert them. ``apply=False`` (the default) only reports
    planned candidates; it never calls ``upsert_relationship``.
    """
    db_path = _require_db(db_path)

    if not apply:
        conn = _readonly_connection(db_path)
        try:
            candidates = _candidate_cooccurrence_pairs(conn)
        finally:
            conn.close()
        by_namespace: dict[str, int] = {}
        for c in candidates:
            by_namespace[c["namespace"]] = by_namespace.get(c["namespace"], 0) + 1
        return MaintenanceResult(
            ok=True, dry_run=True, operation="backfill-relationships", db_path=str(db_path),
            counts={
                "candidates_planned": len(candidates),
                "namespaces_affected": len(by_namespace),
            },
        )

    with maintenance_lock(db_path):
        backup_info = _backup_and_verify(db_path, hermes_home)
        if not backup_info["ok"]:
            return MaintenanceResult(
                ok=False, dry_run=False, operation="backfill-relationships", db_path=str(db_path),
                backup=backup_info, issues=[f"backup verification failed: {backup_info['reason']}"],
            )

        store = MemoryStore(db_path=db_path, hrr_dim=hrr_dim)
        try:
            conn = store._conn
            candidates = _candidate_cooccurrence_pairs(conn)
            written = 0
            for c in candidates:
                relationships_module.upsert_relationship(
                    store,
                    c["source_entity_id"], c["target_entity_id"], _RELATIONSHIP_TYPE,
                    namespace=c["namespace"], strength=_RELATIONSHIP_STRENGTH,
                    confidence=_RELATIONSHIP_CONFIDENCE, extraction_method=_EXTRACTION_METHOD,
                    source_fact_id=c["fact_id"], target_fact_id=c["fact_id"],
                )
                written += 1
        finally:
            store.close()

        return MaintenanceResult(
            ok=True, dry_run=False, operation="backfill-relationships", db_path=str(db_path),
            counts={"relationships_written": written}, backup=backup_info,
        )


# ---------------------------------------------------------------------------
# verify — thin alias for diagnostics.run_diagnostics
# ---------------------------------------------------------------------------

def verify(db_path: "str | Path", *, backup_path: "str | Path | None" = None):
    """`hermes memory verify` is intentionally a thin alias for
    ``diagnostics.run_diagnostics`` — same read-only checks `hermes memory
    health` already runs, under the maintenance-command name the
    implementation plan also allows ("Add `hermes memory verify` or reuse
    health consistently"). Kept here (rather than skipping it) so Phase 6's
    required command list is satisfied literally, without duplicating any
    check logic.
    """
    from . import diagnostics as diagnostics_module

    return diagnostics_module.run_diagnostics(db_path, backup_path=backup_path)
