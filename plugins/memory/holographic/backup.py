"""SQLite backup/restore utilities for PR 3 production hardening.

Pure functions operating on file paths — no dependency on a live
MemoryStore/HolographicMemoryProvider instance, so they can run safely
against a closed database file. The provider orchestrates calling these
(see HolographicMemoryProvider.backup()/restore_from_backup()); restore is
deliberately NOT exposed as a model-callable `fact_store` tool action — see
the module docstring note in __init__.py for why.

Uses sqlite3.Connection.backup() (the SQLite online backup API) rather than
copying the file, so a concurrently-written WAL-mode database still yields
a consistent snapshot without pausing writers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_FILENAME_RE = re.compile(r"^memory-(\d{8}-\d{6})(?:-(\d+))?\.db$")


@dataclass
class BackupInfo:
    path: str
    sidecar_path: str
    checksum: str
    timestamp: str
    fact_count: int


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""


@dataclass
class RestoreResult:
    ok: bool
    reason: str = ""
    reactivated_blocked: int = 0
    safety_copy_path: str = ""


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(db_path: str, backup_dir: str) -> BackupInfo:
    """Snapshot db_path into backup_dir using SQLite's online backup API.

    Safe against a concurrently-open WAL-mode connection — this does NOT
    require the caller to close their MemoryStore first (unlike restore,
    which does). Writes a `<file>.json` sidecar with checksum/metadata and
    sets restrictive (0600) permissions on both files.
    """
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    dest_path = str(Path(backup_dir) / f"memory-{timestamp}.db")
    # Disambiguate same-second backups (e.g. rapid manual calls, or tests)
    # rather than silently overwriting an earlier one.
    suffix = 1
    while os.path.exists(dest_path):
        suffix += 1
        dest_path = str(Path(backup_dir) / f"memory-{timestamp}-{suffix}.db")

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    os.chmod(dest_path, 0o600)
    checksum = _sha256_file(dest_path)

    conn = sqlite3.connect(dest_path)
    try:
        fact_count = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
    finally:
        conn.close()

    sidecar_path = dest_path + ".json"
    sidecar = {
        "timestamp": timestamp,
        "checksum_sha256": checksum,
        "fact_count": fact_count,
        "source_db": os.path.basename(db_path),
    }
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
    os.chmod(sidecar_path, 0o600)

    return BackupInfo(path=dest_path, sidecar_path=sidecar_path, checksum=checksum,
                       timestamp=timestamp, fact_count=fact_count)


def verify_backup(backup_path: str) -> VerifyResult:
    """Checksum + SQLite integrity_check a backup file against its sidecar."""
    if not os.path.exists(backup_path):
        return VerifyResult(ok=False, reason="backup file does not exist")

    sidecar_path = backup_path + ".json"
    if not os.path.exists(sidecar_path):
        return VerifyResult(ok=False, reason="missing sidecar metadata file")

    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
    except Exception as exc:
        return VerifyResult(ok=False, reason=f"unreadable sidecar: {exc}")

    expected = sidecar.get("checksum_sha256")
    if not expected:
        return VerifyResult(ok=False, reason="sidecar missing checksum")

    actual = _sha256_file(backup_path)
    if actual != expected:
        return VerifyResult(ok=False, reason="checksum mismatch — backup file may be corrupted or tampered with")

    try:
        conn = sqlite3.connect(backup_path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        return VerifyResult(ok=False, reason=f"could not open backup for integrity check: {exc}")

    if result != "ok":
        return VerifyResult(ok=False, reason=f"PRAGMA integrity_check failed: {result}")

    return VerifyResult(ok=True)


def restore_backup(
    backup_path: str,
    target_db_path: str,
    *,
    tombstones: frozenset[str] = frozenset(),
) -> RestoreResult:
    """Restore backup_path onto target_db_path, deletion-aware and safe.

    Precondition: the caller must have already closed any live MemoryStore
    on target_db_path — this does file-level replacement, which an open
    WAL-mode connection could otherwise corrupt or silently ignore.

    Order of operations (fail-closed at every step — the live DB is never
    touched until the backup is fully validated):
      1. verify_backup() — checksum + integrity_check on the backup itself.
      2. Copy the backup into a staging file and integrity_check AGAIN there
         (defends against a checksum that matched but filesystem corruption
         introduced during the copy itself).
      3. Delete-sweep the staging copy: any fact whose content hash is in
         `tombstones` (facts the user explicitly forgot AFTER this backup
         was taken, read from the live DB by the caller before closing it)
         is removed again — so restoring an old backup can't silently
         reactivate something the user deleted.
      4. Make a timestamped safety copy of the CURRENT target_db_path (if it
         exists) — never destroyed by this function, so a bad restore is
         always recoverable.
      5. Atomically swap the validated staging file into target_db_path.
    """
    verified = verify_backup(backup_path)
    if not verified.ok:
        return RestoreResult(ok=False, reason=f"backup failed verification: {verified.reason}")

    staging_path = target_db_path + ".restore-staging"
    for ext in ("", "-wal", "-shm"):
        stale = staging_path + ext
        if os.path.exists(stale):
            os.remove(stale)
    shutil.copy2(backup_path, staging_path)

    try:
        conn = sqlite3.connect(staging_path)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                return RestoreResult(ok=False, reason=f"staged copy failed integrity_check: {result}")

            reactivated_blocked = 0
            if tombstones:
                rows = conn.execute("SELECT fact_id, content FROM facts").fetchall()
                from .store import content_hash  # local import: avoid a hard module-level cycle
                to_delete = [r[0] for r in rows if content_hash(r[1]) in tombstones]
                if to_delete:
                    placeholders = ",".join("?" * len(to_delete))
                    conn.execute(f"DELETE FROM fact_entities WHERE fact_id IN ({placeholders})", to_delete)
                    conn.execute(f"DELETE FROM facts WHERE fact_id IN ({placeholders})", to_delete)
                    conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
                    conn.commit()
                    reactivated_blocked = len(to_delete)
        finally:
            conn.close()
    except Exception as exc:
        return RestoreResult(ok=False, reason=f"staging validation failed: {exc}")

    safety_copy_path = ""
    if os.path.exists(target_db_path):
        safety_copy_path = f"{target_db_path}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(target_db_path, safety_copy_path)
        os.chmod(safety_copy_path, 0o600)
        for ext in ("-wal", "-shm"):
            sidecar = target_db_path + ext
            if os.path.exists(sidecar):
                os.remove(sidecar)

    os.replace(staging_path, target_db_path)
    os.chmod(target_db_path, 0o600)

    return RestoreResult(ok=True, reactivated_blocked=reactivated_blocked, safety_copy_path=safety_copy_path)


def prune_backups(backup_dir: str, keep: int) -> list[str]:
    """Keep the `keep` most recent backups (by filename timestamp), remove the rest.

    Returns the paths removed. A non-positive `keep` is treated as "keep
    all" (a no-op) — pruning is opt-in, never surprising.
    """
    if keep <= 0:
        return []
    if not os.path.isdir(backup_dir):
        return []

    candidates = []
    for name in os.listdir(backup_dir):
        m = _FILENAME_RE.match(name)
        if m:
            suffix = int(m.group(2)) if m.group(2) else 1
            candidates.append((m.group(1), suffix, name))
    candidates.sort(reverse=True)  # newest timestamp (then suffix) first

    removed = []
    for _, _, name in candidates[keep:]:
        path = str(Path(backup_dir) / name)
        sidecar = path + ".json"
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            continue
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError:
                pass
    return removed
