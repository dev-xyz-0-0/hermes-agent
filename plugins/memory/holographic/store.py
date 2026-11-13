"""
SQLite-backed fact store with entity resolution and trust scoring.
Single-user Hermes memory store plugin.
"""

import contextlib
import hashlib
import re
import sqlite3
import threading
from pathlib import Path

try:
    from . import holographic as hrr
except ImportError:
    import holographic as hrr  # type: ignore[no-redef]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB,
    status          TEXT DEFAULT 'active',
    superseded_by   INTEGER,
    source_type     TEXT DEFAULT '',
    extraction_method TEXT DEFAULT '',
    confidence      REAL DEFAULT 1.0,
    namespace       TEXT DEFAULT ''
);

-- Content-uniqueness index intentionally NOT created here (see _init_db —
-- it must run after namespace is guaranteed to exist on BOTH fresh and
-- pre-existing-table databases, which an unconditional top-of-script
-- CREATE INDEX can't guarantee for the latter).

-- Tombstones for explicitly forgotten facts (PR 3 deletion-aware restore):
-- restoring an older backup must not silently reactivate a fact the user
-- deleted after that backup was taken. Keyed by content hash, not fact_id,
-- since a restored row gets a different fact_id.
CREATE TABLE IF NOT EXISTS forgotten_facts (
    content_hash TEXT NOT NULL,
    namespace    TEXT NOT NULL DEFAULT '',
    forgotten_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (content_hash, namespace)
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE TABLE IF NOT EXISTS fact_sources (
    source_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id    INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
    via        TEXT DEFAULT '',
    platform   TEXT DEFAULT '',
    chat_id    TEXT DEFAULT '',
    thread_id  TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    author     TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);
CREATE INDEX IF NOT EXISTS idx_fact_sources_fact_id ON fact_sources(fact_id);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Phase 10 (demo-reference integration, "Database migrations") — explicit
-- internal schema-version tracking. See CURRENT_SCHEMA_VERSION below for the
-- versioning scheme and MemoryStore._detect_schema_generation for how a
-- database that predates this table entirely (every pre-PR2/pre-PR3
-- database does, by definition) is still correctly classified.
--
-- Downgrade note: this table is purely additive. Older plugin code (any
-- version before this phase) has no SQL statement anywhere that references
-- `schema_metadata` — it neither SELECTs, INSERTs, UPDATEs, nor DROPs it —
-- so reopening a migrated database with OLDER code is safe: the table is
-- silently ignored, exactly like `entity_relationships` is ignored by code
-- older than Phase 5. The reverse is NOT supported: this phase's migration
-- logic does not attempt to convert a current-schema database back into a
-- pre-PR2/pre-PR3 shape, and no rollback path for that exists or is
-- planned.
CREATE TABLE IF NOT EXISTS schema_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Phase 5 (demo-reference integration) — namespace-scoped entity
-- relationships. Unlike `fact_entities`/`entities` (which remain shared,
-- global-ID resources — see _resolve_entity), relationship OWNERSHIP is
-- always namespace-scoped: two chats that happen to resolve the same entity
-- NAME to the same global entity_id must never see each other's edges. The
-- composite unique index below is the mechanism — every read/write in
-- relationships.py filters by namespace explicitly, it never infers scope
-- from the entity row. `status` mirrors `facts.status` ('active' |
-- 'stale') so a relationship derived from a since-superseded fact can be
-- marked stale without being hard-deleted (see
-- MemoryStore.mark_relationships_stale_for_fact), while a relationship
-- derived from a FORGOTTEN fact is hard-deleted (see
-- MemoryStore.invalidate_relationships_for_fact) — forgetting is
-- intentionally irreversible, matching remove_fact's own tombstone policy
-- for the fact it was derived from.
CREATE TABLE IF NOT EXISTS entity_relationships (
    relationship_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entity_id  INTEGER NOT NULL REFERENCES entities(entity_id),
    target_entity_id  INTEGER NOT NULL REFERENCES entities(entity_id),
    relationship_type TEXT NOT NULL,
    namespace         TEXT NOT NULL DEFAULT '',
    strength          REAL DEFAULT 0.5,
    confidence        REAL DEFAULT 1.0,
    extraction_method TEXT DEFAULT '',
    source_fact_id    INTEGER REFERENCES facts(fact_id),
    target_fact_id    INTEGER REFERENCES facts(fact_id),
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_entity_relationships_source ON entity_relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_relationships_target ON entity_relationships(target_entity_id);
-- The namespace index and the namespace-scoped composite unique index are
-- intentionally NOT created here, for the same reason documented above for
-- facts.idx_facts_namespace: a pre-existing entity_relationships table
-- (see MemoryStore._migrate_entity_relationships) might not have a
-- `namespace` column yet, and this script runs unconditionally on every
-- startup via `executescript` — referencing a not-yet-existing column here
-- would raise before the ALTER TABLE migration ever runs.

-- Phase 8 (demo-reference integration, "Retrieval telemetry") — optional,
-- content-free retrieval-event log. See telemetry.py's module docstring for
-- the full column-by-column privacy rationale. Summary: every column here
-- is either an opaque identifier (event_id is a random UUID, never derived
-- from query text; namespace_digest is the same one-way SHA-1 digest
-- diagnostics.py's _namespace_digest already uses for standard-output
-- namespace counts), a bare already-local integer (fact_id — meaningless
-- outside this one database file and already exposed by every other
-- fact_store action), a numeric score/multiplier/rank, a nullable boolean
-- feedback flag, a timestamp, or an algorithm-version string. No prompts,
-- fact text, entity/person names, chat IDs, raw namespaces, or credentials
-- are ever written here. Off by default (HolographicMemoryProvider.
-- _retrieval_telemetry defaults false) and, even when enabled, bounded by a
-- row-count cap enforced on every insert (see MemoryStore.
-- prune_retrieval_events / the retrieval_telemetry_retention config key).
-- Purely additive and optional, like entity_relationships/schema_metadata
-- above: older code that doesn't know about this table simply never
-- queries it.
CREATE TABLE IF NOT EXISTS retrieval_events (
    event_id            TEXT PRIMARY KEY,
    namespace_digest    TEXT NOT NULL DEFAULT '',
    fact_id             INTEGER,
    fts_score           REAL,
    jaccard_score       REAL,
    hrr_score           REAL,
    temporal_multiplier REAL,
    trust_multiplier    REAL,
    final_rank          INTEGER,
    helpful             INTEGER,
    algorithm_version   TEXT NOT NULL DEFAULT '1',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_fact_ns ON retrieval_events(fact_id, namespace_digest);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_created_at ON retrieval_events(created_at);
"""

# ---------------------------------------------------------------------------
# Schema versioning (Phase 10 — "Database migrations")
# ---------------------------------------------------------------------------
# Versioning scheme: a single monotonically-increasing integer, stored as
# TEXT in schema_metadata['schema_version']. Bump it by exactly 1 whenever
# this module's on-disk schema changes in a way future code needs to detect
# directly (new table/column/constraint that isn't already self-describing
# via PRAGMA table_info/index_list introspection). "1" is the version as of
# this phase — the FIRST version this plugin tracks explicitly. Every
# database that predates this constant's introduction (every pre-PR2,
# pre-PR3, and "current-as-of-Phase-6" database) simply has no
# schema_metadata row at all; MemoryStore._detect_schema_generation()
# disambiguates those by introspecting the actual facts/entity_relationships
# table shapes (never by trusting the row's absence alone to mean any one
# specific generation), and _finalize_schema_version() backfills the row
# once the database is confirmed current.
CURRENT_SCHEMA_VERSION = "1"


class MigrationValidationError(RuntimeError):
    """Raised when a destructive schema migration fails its pre-commit
    safety gate (``PRAGMA foreign_key_check`` and/or a facts/FTS row-count
    parity check).

    Always raised *before* the migration's transaction is committed, and
    always from inside a ``try`` block whose ``except`` clause calls
    ``self._conn.rollback()`` — so raising this leaves the on-disk database
    exactly as it was before the migration attempt started. See
    ``MemoryStore._migrate_drop_global_content_unique`` and
    ``MemoryStore._validate_facts_rebuild``.
    """


# Trust adjustment constants
_HELPFUL_DELTA   =  0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN       =  0.0
_TRUST_MAX       =  1.0

# Entity extraction patterns
_RE_CAPITALIZED  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA          = re.compile(
    r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)',
    re.IGNORECASE,
)


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))


def bank_name(namespace: str, category: str) -> str:
    """Deterministic, opaque memory-bank key for a (namespace, category) pair.

    ``namespace == ""`` is the unscoped/CLI identity and keeps the original
    ``cat:<category>`` key so pre-existing unscoped banks (written before
    Phase 2) remain usable without any migration. Every other namespace is
    folded through a short SHA-1 digest — never the raw namespace string —
    so a bank row never reveals a chat ID or other raw identity, matching
    the precedent in diagnostics.py's ``_namespace_digest``.
    """
    if not namespace:
        return f"cat:{category}"
    digest = hashlib.sha1(namespace.encode("utf-8")).hexdigest()[:16]
    return f"cat:ns:{digest}:{category}"


def content_hash(content: str) -> str:
    """Stable identity for a fact's text, used for forget tombstones.

    Tombstones key on this (not fact_id) because a fact restored from an
    older backup gets a NEW fact_id — the content is the only thing that
    survives across a delete/backup/restore cycle to recognize "this is the
    thing the user asked to forget."
    """
    return hashlib.sha256((content or "").strip().encode("utf-8")).hexdigest()


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring."""

    def __init__(
        self,
        db_path: "str | Path | None" = None,
        default_trust: float = 0.5,
        hrr_dim: int = 1024,
    ) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self.hrr_dim = hrr_dim
        self._hrr_available = hrr._HAS_NUMPY
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        # Phase 4: depth counter for `transaction()`. While > 0, internal
        # write paths defer their usual per-call commit to `_maybe_commit()`
        # so a batch of mutations commits or rolls back as one unit instead
        # of nesting commits (see `transaction()`/`_maybe_commit` below).
        self._txn_depth = 0
        # Phase 4: when > 0, `rebuild_bank()` just records the
        # (namespace, category) pair instead of recomputing immediately —
        # see `defer_bank_rebuilds()`. Lets a batch of N mutations against
        # the same bank(s) rebuild each affected bank once instead of N
        # times, without changing what add_fact/update_fact/supersede_fact/
        # remove_fact call internally.
        self._defer_bank_rebuild_depth = 0
        self._pending_bank_rebuilds: set[tuple[str, str]] = set()
        self._init_db()

    # ------------------------------------------------------------------
    # Transaction primitives (Phase 4 — batch ingestion)
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        """Run a block of store mutations as a single SQLite transaction.

        Every public write method below normally calls ``self._conn.commit()``
        right after its own statement(s) so a lone call is durable
        immediately. Calling several of them back-to-back would therefore
        nest commits — each one durable on its own, with no way to undo an
        earlier one if a later call fails. Inside this context those internal
        commits become no-ops (see `_maybe_commit`); only the outermost
        `transaction()` block commits (on success) or rolls back (on any
        exception), so the whole block is atomic.

        Reentrant/nestable: a nested `transaction()` call just increments the
        depth counter and lets the outermost one own the actual commit/
        rollback, so helper code that opens its own `transaction()` is safe
        to call from inside a caller's own `transaction()` block.

        Held under `self._lock` (an RLock) for its entire duration, exactly
        like every other store method — a concurrent caller on another
        thread simply waits for the whole batch to finish rather than
        interleaving with it, matching the store's existing locking pattern.
        """
        with self._lock:
            self._txn_depth += 1
            is_outer = self._txn_depth == 1
            try:
                yield self
                if is_outer:
                    self._conn.commit()
            except Exception:
                if is_outer:
                    self._conn.rollback()
                raise
            finally:
                self._txn_depth -= 1

    def _maybe_commit(self) -> None:
        """Commit now, unless an outer `transaction()` block owns the commit."""
        if self._txn_depth == 0:
            self._conn.commit()

    @contextlib.contextmanager
    def defer_bank_rebuilds(self):
        """Collapse repeated `rebuild_bank()` calls into one per pair.

        Every mutation method (`add_fact`, `update_fact`, `supersede_fact`,
        `remove_fact`) calls `rebuild_bank()` itself after writing — correct
        and cheap for a single call, but wasteful inside a batch of N
        mutations touching the same (namespace, category) pair. Inside this
        context, `rebuild_bank()` just records the pair; the actual rebuild
        runs once per distinct pair when the outermost `defer_bank_rebuilds()`
        block exits successfully. On an exception, pending pairs are
        discarded without rebuilding (the whole point in the common case is
        running inside a `transaction()` block that is about to roll back
        anyway). Nestable, like `transaction()`.
        """
        with self._lock:
            self._defer_bank_rebuild_depth += 1
            failed = False
            try:
                yield
            except Exception:
                failed = True
                raise
            finally:
                self._defer_bank_rebuild_depth -= 1
                if self._defer_bank_rebuild_depth == 0:
                    pending = self._pending_bank_rebuilds
                    self._pending_bank_rebuilds = set()
                    if not failed:
                        for ns, cat in pending:
                            self.rebuild_bank(cat, ns)

    @contextlib.contextmanager
    def _savepoint(self, name: str):
        """A SQLite SAVEPOINT that only undoes its own block.

        Unlike `self._conn.rollback()` (which would abort an entire
        in-progress `transaction()`), rolling back to a savepoint undoes
        only the statements executed since it was created — used for the
        store's existing "try an insert, fall back to the pre-existing row
        on a collision" recovery pattern (see `supersede_fact`) so that
        recovery stays safe to run inside a batch transaction.
        """
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self._conn.execute(f"RELEASE SAVEPOINT {name}")

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables, indexes, and triggers if they do not exist."""
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        # Classify the on-disk schema BEFORE `executescript(_SCHEMA)` below
        # creates `facts` et al. for a brand-new database file — otherwise
        # a fresh database would already look "current" by the time
        # `_detect_schema_generation()` ran. Purely from introspection
        # (PRAGMA table_info/index_list), never from the mere presence/
        # absence of schema_metadata alone. Exposed as an attribute (not
        # persisted) so tests can assert the detector's classification
        # directly against the Phase 0 pre_pr2_db/pre_pr3_db fixtures.
        was_fresh = not self._table_exists("facts")
        self._conn.executescript(_SCHEMA)
        self._last_detected_schema_generation = "fresh" if was_fresh else self._detect_schema_generation()
        # Migrate: add columns if missing (safe, idempotent, for existing databases).
        # SQLite backfills a DEFAULT value into pre-existing rows on ADD COLUMN,
        # so legacy facts become status='active' automatically — no data rewrite.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "hrr_vector" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")
        if "status" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN status TEXT DEFAULT 'active'")
        if "superseded_by" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN superseded_by INTEGER")
        if "source_type" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN source_type TEXT DEFAULT ''")
        if "extraction_method" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN extraction_method TEXT DEFAULT ''")
        if "confidence" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN confidence REAL DEFAULT 1.0")
        if "namespace" not in columns:
            # PR 3: isolates facts by chat/profile identity. Default '' means
            # "unscoped" — every pre-existing fact (and every single-tenant
            # CLI user, who never computes a non-empty namespace) keeps
            # working exactly as before. Only multi-tenant gateway sessions
            # (which DO compute a non-empty namespace) stop seeing each
            # other's facts — see HolographicMemoryProvider._namespace.
            self._conn.execute("ALTER TABLE facts ADD COLUMN namespace TEXT DEFAULT ''")
        self._conn.commit()
        # Now that `namespace` is guaranteed to exist (fresh table had it
        # inline; pre-existing table just got it ALTERed in), it's safe to
        # index it. The composite unique index is also (re)created inside
        # _migrate_drop_global_content_unique()'s rebuild when that runs —
        # these are idempotent no-ops in that case.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_namespace ON facts(namespace)")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_content_namespace ON facts(content, namespace)")
        self._conn.commit()
        self._migrate_drop_global_content_unique()
        self._migrate_entity_relationships()
        self._finalize_schema_version()

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _has_unique_on_content_alone(self) -> bool:
        """True if `facts` still carries the pre-PR3 global UNIQUE(content)
        constraint (an implicit index from the old inline column
        constraint). Shared by `_detect_schema_generation` and
        `_migrate_drop_global_content_unique` so both use exactly the same
        test."""
        for idx in self._conn.execute("PRAGMA index_list(facts)").fetchall():
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in self._conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
            if cols == ["content"]:
                return True
        return False

    def _detect_schema_generation(self) -> str:
        """Classify the on-disk schema purely from introspection.

        Returns one of:
          - ``"fresh"``    — no `facts` table yet (brand-new database file).
          - ``"pre_pr2"``  — the oldest shape: global UNIQUE(content) and
            none of PR2's status/provenance columns yet.
          - ``"pre_pr3"``  — PR2's status/provenance columns already exist
            (added by ALTER TABLE, possibly by this very call if `facts`
            was just classified "pre_pr2" moments ago and the ALTERs below
            already ran once in a prior open) but the global
            UNIQUE(content) constraint and `namespace` column are not both
            resolved yet.
          - ``"current"``  — neither legacy trait remains: no content-alone
            unique index, and `namespace` is present.

        Never trusts `schema_metadata` — a real pre-PR2/pre-PR3 database
        predates that table's existence entirely, so this always
        introspects `PRAGMA table_info(facts)` / `PRAGMA index_list(facts)`
        directly, per implementation-plan.md section 7.
        """
        if not self._table_exists("facts"):
            return "fresh"
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        has_pr2_columns = "status" in columns and "superseded_by" in columns
        has_namespace = "namespace" in columns
        unique_on_content_alone = self._has_unique_on_content_alone()
        if not has_pr2_columns:
            # A database with the global-unique constraint already gone
            # but still missing PR2's columns should not exist in practice
            # (PR2 shipped before PR3), but if it ever did, there is
            # nothing destructive left to do for it here beyond the
            # column ALTERs _init_db already runs unconditionally, so it
            # is reported the same as a true pre-PR2 database.
            return "pre_pr2"
        if unique_on_content_alone or not has_namespace:
            return "pre_pr3"
        return "current"

    def _finalize_schema_version(self) -> None:
        """Write schema_metadata['schema_version'] once the database is
        confirmed current — a true no-op (zero writes) if it already holds
        CURRENT_SCHEMA_VERSION, which is what keeps re-opening an
        already-current database from registering as a change on every
        single startup (see the migration-idempotency test)."""
        row = self._conn.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None and row[0] == CURRENT_SCHEMA_VERSION:
            return
        self._conn.execute(
            "INSERT INTO schema_metadata (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CURRENT_SCHEMA_VERSION,),
        )
        self._conn.commit()

    def get_schema_version(self) -> "str | None":
        """Public accessor for the tracked schema version (None if this
        database has never been finalized as current — should not happen
        for any database opened through this class, since __init__ always
        calls _finalize_schema_version() before returning)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            return row[0] if row is not None else None

    def _migrate_entity_relationships(self) -> None:
        """Defensive, idempotent migration for `entity_relationships`.

        A fresh database gets every column inline from `_SCHEMA` above, so
        this is normally a no-op. It exists only to safely backfill
        `namespace`/`status`/etc. on a differently-shaped `entity_relationships`
        table that might already exist (e.g. a database touched by a future
        or experimental build before this phase). Per implementation-plan.md
        Phase 5 / "7. Database Migration Plan": pre-existing rows become
        unscoped (namespace='') rather than being broadcast into every
        namespace — they simply keep the default the ALTER backfills.
        """
        if not self._table_exists("entity_relationships"):
            return
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(entity_relationships)").fetchall()}
        if "namespace" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN namespace TEXT NOT NULL DEFAULT ''")
        if "strength" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN strength REAL DEFAULT 0.5")
        if "confidence" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN confidence REAL DEFAULT 1.0")
        if "extraction_method" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN extraction_method TEXT DEFAULT ''")
        if "source_fact_id" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN source_fact_id INTEGER")
        if "target_fact_id" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN target_fact_id INTEGER")
        if "status" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "created_at" not in columns:
            # SQLite's ALTER TABLE ADD COLUMN rejects a non-constant default
            # (CURRENT_TIMESTAMP) — unlike the inline _SCHEMA definition
            # above, which CAN use it because it's part of CREATE TABLE.
            # Backfilled rows just get NULL; new rows written after this
            # migration go through upsert_relationship's INSERT, which
            # relies on this same column default only on fresh tables.
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN created_at TIMESTAMP")
        if "updated_at" not in columns:
            self._conn.execute("ALTER TABLE entity_relationships ADD COLUMN updated_at TIMESTAMP")
        self._conn.commit()

        # Composite unique index — the namespace-scoping mechanism itself
        # (implementation-plan.md Phase 5 "Schema"). Wrapped defensively: if
        # a pre-existing table already has colliding (namespace, source,
        # target, type) duplicates (only possible on a legacy/foreign
        # table — a fresh table created by this module never can), creating
        # the index would fail; log and continue rather than block startup,
        # since this table is additive and unused until relationships.py
        # writes to it.
        try:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_relationships_unique "
                "ON entity_relationships(namespace, source_entity_id, target_entity_id, relationship_type)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entity_relationships_namespace ON entity_relationships(namespace)"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            self._conn.rollback()

    # DDL for the three rebuilt tables, issued as individual `.execute()`
    # calls (NEVER via `executescript()`) inside `_migrate_drop_global_
    # content_unique`. `executescript()` forces an implicit COMMIT of any
    # pending transaction before it runs — which would silently commit the
    # preceding RENAME/DROP TRIGGER statements out from under the
    # surrounding try/except, defeating the whole point of wrapping the
    # rebuild in one rollback-able transaction. Individual `.execute()`
    # calls (including DDL — SQLite's DDL is fully transactional) do not
    # have that quirk, so the entire rebuild below commits or rolls back
    # as one unit. Keep these in sync with the `facts`/`fact_entities`/
    # `fact_sources` definitions in `_SCHEMA` above.
    _REBUILD_FACTS_SQL = """
        CREATE TABLE facts (
            fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content         TEXT NOT NULL,
            category        TEXT DEFAULT 'general',
            tags            TEXT DEFAULT '',
            trust_score     REAL DEFAULT 0.5,
            retrieval_count INTEGER DEFAULT 0,
            helpful_count   INTEGER DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            hrr_vector      BLOB,
            status          TEXT DEFAULT 'active',
            superseded_by   INTEGER,
            source_type     TEXT DEFAULT '',
            extraction_method TEXT DEFAULT '',
            confidence      REAL DEFAULT 1.0,
            namespace       TEXT DEFAULT ''
        )
    """
    _REBUILD_FACT_ENTITIES_SQL = """
        CREATE TABLE fact_entities (
            fact_id   INTEGER REFERENCES facts(fact_id),
            entity_id INTEGER REFERENCES entities(entity_id),
            PRIMARY KEY (fact_id, entity_id)
        )
    """
    _REBUILD_FACT_SOURCES_SQL = """
        CREATE TABLE fact_sources (
            source_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_id    INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE CASCADE,
            via        TEXT DEFAULT '',
            platform   TEXT DEFAULT '',
            chat_id    TEXT DEFAULT '',
            thread_id  TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            author     TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    _REBUILD_FACTS_AI_TRIGGER_SQL = """
        CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
            INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags);
        END
    """
    _REBUILD_FACTS_AD_TRIGGER_SQL = """
        CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
            INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.fact_id, old.content, old.tags);
        END
    """
    _REBUILD_FACTS_AU_TRIGGER_SQL = """
        CREATE TRIGGER facts_au AFTER UPDATE ON facts BEGIN
            INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.fact_id, old.content, old.tags);
            INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags);
        END
    """

    def _validate_facts_rebuild(self, expected_facts: int, expected_fact_sources: int) -> None:
        """Pre-commit safety gate for the facts-table rebuild.

        Must be called after every INSERT/DROP statement of the rebuild has
        already executed, but BEFORE `self._conn.commit()`. Raising
        `MigrationValidationError` here is caught by the caller's `except`
        clause, which rolls back the ENTIRE rebuild transaction (the
        RENAMEs, the DROP TRIGGERs, the table recreation, the data copies,
        and the DROP TABLEs of the renamed originals) — so the on-disk
        database ends up exactly as it was before
        `_migrate_drop_global_content_unique` was called. The caller is
        free to retry on a later open (this whole method is idempotent:
        the content-alone unique index — the trigger for running any of
        this — is still present after a rollback, so the next `_init_db`
        call attempts the rebuild again from scratch).
        """
        fk_violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise MigrationValidationError(
                f"foreign_key_check failed after facts rebuild: {len(fk_violations)} violation(s)"
            )
        actual_facts = int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
        if actual_facts != expected_facts:
            raise MigrationValidationError(
                f"facts row count changed during rebuild: expected {expected_facts}, got {actual_facts}"
            )
        actual_fts = int(self._conn.execute("SELECT COUNT(*) FROM facts_fts_docsize").fetchone()[0])
        if actual_fts != expected_facts:
            raise MigrationValidationError(
                f"facts_fts parity failed after rebuild: {expected_facts} fact(s) but {actual_fts} indexed"
            )
        actual_sources = int(self._conn.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0])
        if actual_sources != expected_fact_sources:
            raise MigrationValidationError(
                f"fact_sources row count changed during rebuild: expected {expected_fact_sources}, got {actual_sources}"
            )

    def _migrate_drop_global_content_unique(self) -> None:
        """One-time rebuild: content must be unique per-namespace, not globally.

        A pre-PR-3 database has an implicit UNIQUE index on facts.content
        alone (from the old inline ``content TEXT NOT NULL UNIQUE`` column
        constraint). That must go — otherwise two different namespaces each
        stating the same fact text would collide on insert. SQLite can't
        alter a column constraint in place, so this rebuilds `facts` (and,
        because SQLite rewrites foreign-key clauses to point at a RENAMEd
        table rather than leaving them pointed at the new one, also rebuilds
        `fact_entities` and `fact_sources` so their FKs end up clean — this
        was verified empirically; rebuilding `facts` alone leaves dangling
        FK references that fail on the next insert). Idempotent: a no-op
        once the content-only unique index is gone.

        The whole rebuild — RENAMEs, trigger drop/recreate, data copy, and
        final DROP TABLEs — runs as a single SQLite transaction, gated by
        `_validate_facts_rebuild()` (PRAGMA foreign_key_check plus a
        facts/FTS/fact_sources row-count parity check) immediately before
        the commit. Any failure there — or any other exception during the
        rebuild — rolls back the whole transaction via the `except` clause
        below, leaving the original `facts`/`fact_entities`/`fact_sources`
        tables (and the pre-existing data in them) completely untouched.
        """
        if not self._has_unique_on_content_alone():
            return

        fk_was_on = bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            old_facts_count = int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
            old_fact_sources_count = (
                int(self._conn.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0])
                if self._table_exists("fact_sources") else 0
            )

            # Explicit BEGIN: Python's sqlite3 module only auto-opens an
            # implicit transaction before DML (INSERT/UPDATE/DELETE/
            # REPLACE) statements, never before DDL. Without this, each
            # ALTER/CREATE/DROP statement below would run in SQLite's own
            # autocommit mode and `self._conn.rollback()` in the `except`
            # clause would have nothing pending to undo — silently leaving
            # a partially rebuilt (and, worse, partially EMPTIED) `facts`
            # table on a validation failure instead of the untouched
            # original. This BEGIN is what makes the whole block one real,
            # rollback-able transaction.
            self._conn.execute("BEGIN")

            self._conn.execute("ALTER TABLE facts RENAME TO facts_pre_ns_unique")
            self._conn.execute("ALTER TABLE fact_entities RENAME TO fact_entities_pre_ns_unique")
            self._conn.execute("ALTER TABLE fact_sources RENAME TO fact_sources_pre_ns_unique")
            # RENAME rewrites trigger/FK bodies to point at the renamed table
            # (verified empirically) — same as the FK issue above. Drop the
            # triggers explicitly so the recreated ones below actually bind
            # to the NEW `facts`, instead of silently no-op'ing against the
            # misbound old name.
            self._conn.execute("DROP TRIGGER IF EXISTS facts_ai")
            self._conn.execute("DROP TRIGGER IF EXISTS facts_ad")
            self._conn.execute("DROP TRIGGER IF EXISTS facts_au")
            # Same issue as the triggers: RENAME keeps each index's NAME
            # attached to the renamed-away table, not the new one. Without
            # dropping them explicitly here, the `CREATE INDEX IF NOT
            # EXISTS` calls below would silently no-op against the
            # old table's indexes — and then `DROP TABLE
            # facts_pre_ns_unique`/`..._fact_sources_pre_ns_unique` further
            # down would delete those indexes entirely, leaving the NEW
            # `facts`/`fact_sources` tables with none of them. Verified
            # empirically.
            for old_index in (
                "idx_facts_trust", "idx_facts_category", "idx_fact_sources_fact_id",
                "idx_facts_namespace", "idx_facts_content_namespace",
            ):
                self._conn.execute(f"DROP INDEX IF EXISTS {old_index}")

            # Recreate facts/fact_entities/fact_sources + FTS triggers via
            # individual `.execute()` calls — see the class-level
            # `_REBUILD_*_SQL` docstring above for why NOT `executescript()`.
            self._conn.execute(self._REBUILD_FACTS_SQL)
            self._conn.execute(self._REBUILD_FACT_ENTITIES_SQL)
            self._conn.execute(self._REBUILD_FACT_SOURCES_SQL)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_trust ON facts(trust_score DESC)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_fact_sources_fact_id ON fact_sources(fact_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_namespace ON facts(namespace)")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_content_namespace ON facts(content, namespace)")
            self._conn.execute(self._REBUILD_FACTS_AI_TRIGGER_SQL)
            self._conn.execute(self._REBUILD_FACTS_AD_TRIGGER_SQL)
            self._conn.execute(self._REBUILD_FACTS_AU_TRIGGER_SQL)

            self._conn.execute("""
                INSERT INTO facts (fact_id, content, category, tags, trust_score,
                                    retrieval_count, helpful_count, created_at, updated_at,
                                    hrr_vector, status, superseded_by, source_type,
                                    extraction_method, confidence, namespace)
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at,
                       hrr_vector, status, superseded_by, source_type,
                       extraction_method, confidence, namespace
                FROM facts_pre_ns_unique
            """)
            self._conn.execute("INSERT INTO fact_entities SELECT * FROM fact_entities_pre_ns_unique")
            self._conn.execute("INSERT INTO fact_sources SELECT * FROM fact_sources_pre_ns_unique")

            self._conn.execute("DROP TABLE facts_pre_ns_unique")
            self._conn.execute("DROP TABLE fact_entities_pre_ns_unique")
            self._conn.execute("DROP TABLE fact_sources_pre_ns_unique")
            # facts_fts is an external-content FTS5 index over `facts` — the
            # table it pointed at is gone, so resync it from the new one.
            self._conn.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")

            self._validate_facts_rebuild(old_facts_count, old_fact_sources_count)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute(f"PRAGMA foreign_keys={'ON' if fk_was_on else 'OFF'}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_fact(
        self,
        content: str,
        category: str = "general",
        tags: str = "",
        source_type: str = "",
        extraction_method: str = "",
        confidence: float = 1.0,
        namespace: str = "",
    ) -> int:
        """Insert a fact and return its fact_id.

        Deduplicates by (content, namespace) (UNIQUE constraint) — the same
        text in two different namespaces is two different rows, by design
        (PR 3 isolation). On duplicate within the same namespace, returns
        the existing fact_id without modifying the row. Extracts entities
        from the content and links them to the fact.
        """
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")

            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO facts (content, category, tags, trust_score,
                                        source_type, extraction_method, confidence, namespace)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (content, category, tags, self.default_trust,
                     source_type, extraction_method, confidence, namespace),
                )
                self._maybe_commit()
                fact_id: int = cur.lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                # Duplicate (content, namespace) — return existing id
                row = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ? AND namespace = ?", (content, namespace)
                ).fetchone()
                return int(row["fact_id"])

            # Entity extraction and linking
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)

            # Compute HRR vector after entity linking
            self._compute_hrr_vector(fact_id, content)
            self.rebuild_bank(category, namespace)

            return fact_id

    def search_facts(
        self,
        query: str,
        category: str | None = None,
        min_trust: float = 0.3,
        limit: int = 10,
        include_superseded: bool = False,
        namespace: str | None = None,
    ) -> list[dict]:
        """Full-text search over facts using FTS5.

        Returns a list of fact dicts ordered by FTS5 rank, then trust_score
        descending. Also increments retrieval_count for matched facts.
        Superseded facts are excluded unless include_superseded=True (history).

        namespace=None means "no namespace filter" — MemoryStore is
        namespace-AWARE but not namespace-ENFORCING; HolographicMemoryProvider
        is the trust boundary and always passes its own namespace explicitly.
        """
        with self._lock:
            query = query.strip()
            if not query:
                return []

            params: list = [query, min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND f.category = ?"
                params.append(category)
            namespace_clause = ""
            if namespace is not None:
                namespace_clause = "AND f.namespace = ?"
                params.append(namespace)
            status_clause = "" if include_superseded else "AND f.status != 'superseded'"
            params.append(limit)

            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at, f.status, f.superseded_by, f.namespace
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ?
                  AND f.trust_score >= ?
                  {category_clause}
                  {namespace_clause}
                  {status_clause}
                ORDER BY fts.rank, f.trust_score DESC
                LIMIT ?
            """

            rows = self._conn.execute(sql, params).fetchall()
            results = [self._row_to_dict(r) for r in rows]

            if results:
                ids = [r["fact_id"] for r in results]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
                    ids,
                )
                self._maybe_commit()

            return results

    def read(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """Run a read-only query and return rows."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def count_facts(self) -> int:
        """Return the number of facts currently stored."""
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    def record_source(
        self,
        fact_id: int,
        *,
        via: str = "",
        platform: str = "",
        chat_id: str = "",
        thread_id: str = "",
        session_id: str = "",
        author: str = "",
    ) -> None:
        """Record where a fact write came from."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fact_sources (fact_id, via, platform, chat_id, thread_id, session_id, author)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (fact_id, via, platform, chat_id, thread_id, session_id, author),
            )
            self._maybe_commit()

    def stats(self, min_trust: float = 0.3, namespace: str | None = None) -> dict:
        """Return operational counts for the store.

        namespace=None (default) reports across ALL namespaces, preserving
        the original contract for existing callers/tests. Pass a namespace
        to scope every count to it (used by the PR 3 diagnostics action).
        """
        with self._lock:
            ns_clause = " WHERE namespace = ?" if namespace is not None else ""
            ns_params = [namespace] if namespace is not None else []
            ns_and = " AND namespace = ?" if namespace is not None else ""

            by_category = {
                row["category"] or "general": row["count"]
                for row in self._conn.execute(
                    f"SELECT category, COUNT(*) AS count FROM facts{ns_clause} GROUP BY category",
                    ns_params,
                ).fetchall()
            }
            return {
                "facts": int(self._conn.execute(f"SELECT COUNT(*) FROM facts{ns_clause}", ns_params).fetchone()[0]),
                "active": int(self._conn.execute(
                    f"SELECT COUNT(*) FROM facts WHERE status != 'superseded'{ns_and}", ns_params
                ).fetchone()[0]),
                "superseded": int(self._conn.execute(
                    f"SELECT COUNT(*) FROM facts WHERE status = 'superseded'{ns_and}", ns_params
                ).fetchone()[0]),
                "entities": int(self._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]),
                "by_category": by_category,
                "recalled_at_least_once": int(
                    self._conn.execute(
                        f"SELECT COUNT(*) FROM facts WHERE retrieval_count > 0{ns_and}", ns_params
                    ).fetchone()[0]
                ),
                "low_trust": int(
                    self._conn.execute(
                        f"SELECT COUNT(*) FROM facts WHERE trust_score < ?{ns_and}",
                        [min_trust] + ns_params,
                    ).fetchone()[0]
                ),
                "namespace": namespace if namespace is not None else "*",
                "database": str(self.db_path),
            }

    def update_fact(
        self,
        fact_id: int,
        content: str | None = None,
        trust_delta: float | None = None,
        tags: str | None = None,
        category: str | None = None,
        namespace: str | None = None,
    ) -> bool:
        """Partially update a fact. Trust is clamped to [0, 1].

        `namespace`, if given, scopes this to facts in that namespace only —
        prevents one chat/profile from tampering with another's fact by
        guessing a fact_id. Returns True if a matching row existed.
        """
        with self._lock:
            if namespace is not None:
                row = self._conn.execute(
                    "SELECT fact_id, trust_score, category, namespace FROM facts WHERE fact_id = ? AND namespace = ?",
                    (fact_id, namespace),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT fact_id, trust_score, category, namespace FROM facts WHERE fact_id = ?", (fact_id,)
                ).fetchone()
            if row is None:
                return False
            fact_namespace = row["namespace"] or ""
            old_category = row["category"]

            assignments: list[str] = ["updated_at = CURRENT_TIMESTAMP"]
            params: list = []

            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)

            params.append(fact_id)
            self._conn.execute(
                f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?",
                params,
            )
            self._maybe_commit()

            # If content changed, re-extract entities
            if content is not None:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, entity_id)
                self._maybe_commit()

            # Recompute HRR vector if content changed
            if content is not None:
                self._compute_hrr_vector(fact_id, content)
            # Rebuild bank(s) for the affected namespace/category pair(s).
            # The fact's own namespace never changes here, so only the
            # category may differ between old and new.
            new_category = category or old_category
            self.rebuild_bank(new_category, fact_namespace)
            if category is not None and category != old_category:
                self.rebuild_bank(old_category, fact_namespace)

            return True

    def supersede_fact(
        self,
        old_fact_id: int,
        new_content: str,
        category: str | None = None,
        tags: str = "",
        source_type: str = "",
        extraction_method: str = "",
        confidence: float = 1.0,
        namespace: str | None = None,
    ) -> int:
        """Atomically replace an active fact with a new one (a correction).

        Inserts new_content as a fresh fact (in `namespace`, if given, else
        old_fact_id's own namespace) and marks old_fact_id as
        status='superseded' pointing to it. The old row is never deleted —
        it stays queryable via include_superseded=True (history) — and
        normal recall/search/list no longer return it.

        If the insert fails for any reason other than a content collision,
        the original fact is left completely untouched (no partial
        supersession). Returns the new (or matched existing) fact_id.
        Raises KeyError if old_fact_id does not exist (optionally scoped to
        `namespace`, so one namespace can't supersede another's fact by id).
        """
        with self._lock:
            if namespace is not None:
                old_row = self._conn.execute(
                    "SELECT fact_id, category, namespace FROM facts WHERE fact_id = ? AND namespace = ?",
                    (old_fact_id, namespace),
                ).fetchone()
            else:
                old_row = self._conn.execute(
                    "SELECT fact_id, category, namespace FROM facts WHERE fact_id = ?", (old_fact_id,)
                ).fetchone()
            if old_row is None:
                raise KeyError(f"fact_id {old_fact_id} not found")

            new_content = new_content.strip()
            if not new_content:
                raise ValueError("new_content must not be empty")
            resolved_category = category or old_row["category"]
            resolved_namespace = namespace if namespace is not None else old_row["namespace"]

            try:
                # Wrapped in a SAVEPOINT (not a bare commit/rollback) so the
                # IntegrityError-recovery branch below only undoes THIS
                # insert attempt, never an enclosing `transaction()` block's
                # earlier, already-successful work (Phase 4 batch safety).
                with self._savepoint("supersede_insert"):
                    cur = self._conn.execute(
                        """
                        INSERT INTO facts (content, category, tags, trust_score,
                                            source_type, extraction_method, confidence, namespace)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (new_content, resolved_category, tags, self.default_trust,
                         source_type, extraction_method, confidence, resolved_namespace),
                    )
                    new_fact_id: int = cur.lastrowid  # type: ignore[assignment]
                    if new_fact_id != old_fact_id:
                        self._conn.execute(
                            "UPDATE facts SET status = 'superseded', superseded_by = ?, "
                            "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                            (new_fact_id, old_fact_id),
                        )
                self._maybe_commit()
            except sqlite3.IntegrityError:
                # new_content collides with an existing fact's UNIQUE
                # (content, namespace) — that fact becomes the replacement
                # instead of inserting a duplicate. The savepoint above
                # already undid the failed insert attempt.
                existing = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ? AND namespace = ?",
                    (new_content, resolved_namespace),
                ).fetchone()
                if existing is None:
                    raise
                new_fact_id = int(existing["fact_id"])
                if new_fact_id != old_fact_id:
                    with self._savepoint("supersede_mark"):
                        self._conn.execute(
                            "UPDATE facts SET status = 'superseded', superseded_by = ?, "
                            "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                            (new_fact_id, old_fact_id),
                        )
                    self._maybe_commit()

            if new_fact_id != old_fact_id:
                for name in self._extract_entities(new_content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(new_fact_id, entity_id)
                self._compute_hrr_vector(new_fact_id, new_content)
                self.rebuild_bank(resolved_category, resolved_namespace)
                if old_row["category"] != resolved_category or old_row["namespace"] != resolved_namespace:
                    self.rebuild_bank(old_row["category"], old_row["namespace"] or "")

            return new_fact_id

    def get_history(
        self,
        category: str | None = None,
        limit: int = 50,
        namespace: str | None = None,
    ) -> list[dict]:
        """Return superseded facts (and what replaced them) for audit/history queries.

        This is the only path that surfaces superseded content — normal
        search/list/prefetch never do.
        """
        with self._lock:
            params: list = []
            category_clause = ""
            if category is not None:
                category_clause = "AND category = ?"
                params.append(category)
            namespace_clause = ""
            if namespace is not None:
                namespace_clause = "AND namespace = ?"
                params.append(namespace)
            params.append(limit)

            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       created_at, updated_at, status, superseded_by, namespace
                FROM facts
                WHERE status = 'superseded'
                  {category_clause}
                  {namespace_clause}
                ORDER BY updated_at DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def remove_fact(self, fact_id: int, namespace: str | None = None, tombstone: bool = True) -> bool:
        """Delete a fact and its entity links. Returns True if a matching row existed.

        `namespace`, if given, scopes the delete (one namespace can't remove
        another's fact by guessing a fact_id). `tombstone=True` (default)
        records the content's hash in `forgotten_facts` so a later restore
        from an older backup can't silently reactivate it — see
        docs/plans/2026-10-15-holographic-pr3-*.md section on deletion-aware
        restore. Pass tombstone=False for internal/non-forget deletes (e.g.
        consolidation collision cleanup) where "the user asked to forget
        this" would not be an accurate record.
        """
        with self._lock:
            if namespace is not None:
                row = self._conn.execute(
                    "SELECT fact_id, category, content, namespace FROM facts WHERE fact_id = ? AND namespace = ?",
                    (fact_id, namespace),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT fact_id, category, content, namespace FROM facts WHERE fact_id = ?", (fact_id,)
                ).fetchone()
            if row is None:
                return False

            self._conn.execute(
                "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
            )
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            if tombstone:
                self._conn.execute(
                    "INSERT OR REPLACE INTO forgotten_facts (content_hash, namespace, forgotten_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (content_hash(row["content"]), row["namespace"]),
                )
            self._maybe_commit()
            self.rebuild_bank(row["category"], row["namespace"] or "")
            return True

    def get_tombstones(self, namespace: str | None = None) -> set[str]:
        """Content hashes explicitly forgotten (optionally scoped to a namespace)."""
        with self._lock:
            if namespace is not None:
                rows = self._conn.execute(
                    "SELECT content_hash FROM forgotten_facts WHERE namespace = ?", (namespace,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT content_hash FROM forgotten_facts").fetchall()
            return {r["content_hash"] for r in rows}

    def list_facts(
        self,
        category: str | None = None,
        min_trust: float = 0.0,
        limit: int = 50,
        include_superseded: bool = False,
        namespace: str | None = None,
    ) -> list[dict]:
        """Browse facts ordered by trust_score descending.

        Optionally filter by category and minimum trust score. Superseded
        facts are excluded unless include_superseded=True (history queries).
        """
        with self._lock:
            params: list = [min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND category = ?"
                params.append(category)
            namespace_clause = ""
            if namespace is not None:
                namespace_clause = "AND namespace = ?"
                params.append(namespace)
            status_clause = "" if include_superseded else "AND status != 'superseded'"
            params.append(limit)

            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at,
                       status, superseded_by, namespace
                FROM facts
                WHERE trust_score >= ?
                  {category_clause}
                  {namespace_clause}
                  {status_clause}
                ORDER BY trust_score DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def record_feedback(self, fact_id: int, helpful: bool, namespace: str | None = None) -> dict:
        """Record user feedback and adjust trust asymmetrically.

        helpful=True  -> trust += 0.05, helpful_count += 1
        helpful=False -> trust -= 0.10

        `namespace`, if given, scopes this to the matching namespace only —
        prevents one chat/profile from nudging another's fact's trust score
        by guessing a fact_id. Returns a dict with fact_id, old_trust,
        new_trust, helpful_count. Raises KeyError if no matching fact exists.
        """
        with self._lock:
            if namespace is not None:
                row = self._conn.execute(
                    "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ? AND namespace = ?",
                    (fact_id, namespace),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?",
                    (fact_id,),
                ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")

            old_trust: float = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)

            helpful_increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score    = ?,
                    helpful_count  = helpful_count + ?,
                    updated_at     = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, helpful_increment, fact_id),
            )
            self._maybe_commit()

            return {
                "fact_id":      fact_id,
                "old_trust":    old_trust,
                "new_trust":    new_trust,
                "helpful_count": row["helpful_count"] + helpful_increment,
            }

    # ------------------------------------------------------------------
    # Entity helpers
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> list[str]:
        """Extract entity candidates from text using simple regex rules.

        Rules applied (in order):
        1. Capitalized multi-word phrases  e.g. "John Doe"
        2. Double-quoted terms             e.g. "Python"
        3. Single-quoted terms             e.g. 'pytest'
        4. AKA patterns                    e.g. "Guido aka BDFL" -> two entities

        Returns a deduplicated list preserving first-seen order.
        """
        seen: set[str] = set()
        candidates: list[str] = []

        def _add(name: str) -> None:
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))

        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))

        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))

        return candidates

    def _resolve_entity(self, name: str) -> int:
        """Find an existing entity by name or alias (case-insensitive) or create one.

        Returns the entity_id.
        """
        # Exact name match
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])

        # Search aliases — aliases stored as comma-separated; use LIKE with % boundaries
        alias_row = self._conn.execute(
            """
            SELECT entity_id FROM entities
            WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'
            """,
            (name,),
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])

        # Create new entity
        cur = self._conn.execute(
            "INSERT INTO entities (name) VALUES (?)", (name,)
        )
        self._maybe_commit()
        return int(cur.lastrowid)  # type: ignore[return-value]

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        """Insert into fact_entities, silently ignore if the link already exists."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO fact_entities (fact_id, entity_id)
            VALUES (?, ?)
            """,
            (fact_id, entity_id),
        )
        self._maybe_commit()

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store HRR vector for a fact. No-op if numpy unavailable."""
        with self._lock:
            if not self._hrr_available:
                return

            # Get entities linked to this fact
            rows = self._conn.execute(
                """
                SELECT e.name FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.entity_id
                WHERE fe.fact_id = ?
                """,
                (fact_id,),
            ).fetchall()
            entities = [row["name"] for row in rows]

            vector = hrr.encode_fact(content, entities, self.hrr_dim)
            self._conn.execute(
                "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
                (hrr.phases_to_bytes(vector), fact_id),
            )
            self._maybe_commit()

    def rebuild_bank(self, category: str, namespace: str = "") -> None:
        """Full rebuild of one (namespace, category) memory bank from its
        active fact vectors.

        Only active (non-superseded) facts contribute — superseded/forgotten
        facts never do, since forgotten facts are hard-deleted from `facts`
        and superseded ones are filtered out here. Scoped to `namespace`
        exactly: a bank never aggregates facts from any other namespace,
        including the unscoped ("") namespace.
        """
        with self._lock:
            if not self._hrr_available:
                return

            if self._defer_bank_rebuild_depth > 0:
                self._pending_bank_rebuilds.add((namespace, category))
                return

            name = bank_name(namespace, category)
            rows = self._conn.execute(
                "SELECT hrr_vector FROM facts "
                "WHERE category = ? AND namespace = ? AND hrr_vector IS NOT NULL "
                "AND status != 'superseded'",
                (category, namespace),
            ).fetchall()

            if not rows:
                self._conn.execute("DELETE FROM memory_banks WHERE bank_name = ?", (name,))
                self._maybe_commit()
                return

            vectors = [hrr.bytes_to_phases(row["hrr_vector"]) for row in rows]
            bank_vector = hrr.bundle(*vectors)
            fact_count = len(vectors)

            # Check SNR
            hrr.snr_estimate(self.hrr_dim, fact_count)

            self._conn.execute(
                """
                INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bank_name) DO UPDATE SET
                    vector = excluded.vector,
                    dim = excluded.dim,
                    fact_count = excluded.fact_count,
                    updated_at = excluded.updated_at
                """,
                (name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, fact_count),
            )
            self._maybe_commit()

    def _rebuild_bank(self, category: str, namespace: str = "") -> None:
        """Back-compat alias for :meth:`rebuild_bank` (internal call sites)."""
        self.rebuild_bank(category, namespace)

    def rebuild_all_vectors(self, dim: int | None = None, namespace: str | None = None) -> int:
        """Recompute HRR vectors + banks from text. For recovery/migration.

        `namespace=None` (default) rebuilds every namespace's vectors and
        banks, matching the pre-Phase-2 contract. Pass a namespace to scope
        the rebuild to just that namespace/category pairs — used by
        maintenance paths that must not touch other tenants' banks.

        Returns the number of facts processed.
        """
        with self._lock:
            if not self._hrr_available:
                return 0

            if dim is not None:
                self.hrr_dim = dim

            if namespace is not None:
                rows = self._conn.execute(
                    "SELECT fact_id, content, category, namespace FROM facts WHERE namespace = ?",
                    (namespace,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT fact_id, content, category, namespace FROM facts"
                ).fetchall()

            pairs: set[tuple[str, str]] = set()
            for row in rows:
                self._compute_hrr_vector(row["fact_id"], row["content"])
                pairs.add((row["namespace"] or "", row["category"]))

            for ns, category in pairs:
                self.rebuild_bank(category, ns)

            return len(rows)

    # ------------------------------------------------------------------
    # Relationship helpers (Phase 5 — namespace-scoped entity relationships)
    # ------------------------------------------------------------------
    # Raw-SQL storage primitives only; the traversal ALGORITHM, shared-memory
    # reuse, and dataclasses live in relationships.py — the same split as
    # retrieval.py (ranking) sits above this module for facts.

    def upsert_relationship(
        self,
        source_entity_id: int,
        target_entity_id: int,
        relationship_type: str,
        *,
        namespace: str = "",
        strength: float = 0.5,
        confidence: float = 1.0,
        extraction_method: str = "",
        source_fact_id: int | None = None,
        target_fact_id: int | None = None,
    ) -> int:
        """Insert or update one namespace-scoped relationship edge.

        Upsert key is the composite unique index
        ``(namespace, source_entity_id, target_entity_id, relationship_type)``
        — direction and type are both part of the key, so A->B "manages" and
        B->A "manages" (or A->B "reports_to") are different rows, never
        merged. A second call with the same key updates
        strength/confidence/extraction_method/source_fact_id/target_fact_id
        in place (never inserts a duplicate row) and reactivates a
        previously 'stale' edge back to 'active', since calling upsert again
        is the caller asserting the edge currently holds.
        """
        relationship_type = (relationship_type or "").strip()
        if not relationship_type:
            raise ValueError("relationship_type must not be empty")
        strength = _clamp_trust(strength)
        confidence = _clamp_trust(confidence)
        namespace = namespace or ""
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO entity_relationships
                        (source_entity_id, target_entity_id, relationship_type, namespace,
                         strength, confidence, extraction_method, source_fact_id, target_fact_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (source_entity_id, target_entity_id, relationship_type, namespace,
                     strength, confidence, extraction_method, source_fact_id, target_fact_id),
                )
                self._maybe_commit()
                return int(cur.lastrowid)  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                self._conn.execute(
                    """
                    UPDATE entity_relationships
                    SET strength = ?, confidence = ?, extraction_method = ?,
                        source_fact_id = ?, target_fact_id = ?, status = 'active',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE namespace = ? AND source_entity_id = ? AND target_entity_id = ?
                          AND relationship_type = ?
                    """,
                    (strength, confidence, extraction_method, source_fact_id, target_fact_id,
                     namespace, source_entity_id, target_entity_id, relationship_type),
                )
                self._maybe_commit()
                row = self._conn.execute(
                    "SELECT relationship_id FROM entity_relationships WHERE namespace = ? "
                    "AND source_entity_id = ? AND target_entity_id = ? AND relationship_type = ?",
                    (namespace, source_entity_id, target_entity_id, relationship_type),
                ).fetchone()
                return int(row["relationship_id"])

    def remove_relationship(
        self, source_entity_id: int, target_entity_id: int, relationship_type: str, namespace: str = "",
    ) -> bool:
        """Hard-delete one relationship edge. Returns True if a row existed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM entity_relationships WHERE namespace = ? AND source_entity_id = ? "
                "AND target_entity_id = ? AND relationship_type = ?",
                (namespace or "", source_entity_id, target_entity_id, relationship_type),
            )
            self._maybe_commit()
            return cur.rowcount > 0

    def list_relationships_for_entity(
        self,
        entity_id: int,
        *,
        namespace: str = "",
        direction: str = "both",
        relationship_type: str | None = None,
        status: str | None = "active",
        limit: int = 200,
    ) -> list[dict]:
        """Relationships touching ``entity_id``, scoped to exactly ``namespace``
        (never any other namespace, including the unscoped/'' one unless
        that IS the namespace asked for)."""
        with self._lock:
            clauses = ["namespace = ?"]
            params: list = [namespace or ""]
            if direction == "outgoing":
                clauses.append("source_entity_id = ?")
                params.append(entity_id)
            elif direction == "incoming":
                clauses.append("target_entity_id = ?")
                params.append(entity_id)
            else:
                clauses.append("(source_entity_id = ? OR target_entity_id = ?)")
                params.extend([entity_id, entity_id])
            if relationship_type is not None:
                clauses.append("relationship_type = ?")
                params.append(relationship_type)
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            params.append(limit)
            sql = f"""
                SELECT relationship_id, source_entity_id, target_entity_id, relationship_type,
                       namespace, strength, confidence, extraction_method, source_fact_id,
                       target_fact_id, status, created_at, updated_at
                FROM entity_relationships
                WHERE {' AND '.join(clauses)}
                ORDER BY relationship_id
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def relationship_neighbors(
        self, entity_ids: list[int], *, namespace: str = "", status: str | None = "active",
    ) -> list[dict]:
        """Every edge touching any of ``entity_ids``, scoped to exactly
        ``namespace``. Used by relationships.py's bounded BFS traversal —
        one query per frontier level, never per-node."""
        if not entity_ids:
            return []
        with self._lock:
            placeholders = ",".join("?" * len(entity_ids))
            params: list = list(entity_ids) + list(entity_ids) + [namespace or ""]
            sql = f"""
                SELECT relationship_id, source_entity_id, target_entity_id, relationship_type,
                       namespace, strength, confidence, status
                FROM entity_relationships
                WHERE (source_entity_id IN ({placeholders}) OR target_entity_id IN ({placeholders}))
                  AND namespace = ?
            """
            if status is not None:
                sql += " AND status = ?"
                params.append(status)
            rows = self._conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def invalidate_relationships_for_fact(self, fact_id: int, namespace: str | None = None) -> int:
        """Hard-delete relationships derived ONLY from ``fact_id``.

        Mirrors `remove_fact`'s own irreversible tombstone policy: once the
        evidence fact is forgotten, the derived edge is gone too, not merely
        marked stale. NOT called automatically by `remove_fact` — the caller
        (provider/batch/test) is responsible for calling this BEFORE its own
        `remove_fact(fact_id, ...)` call, not after: `source_fact_id`/
        `target_fact_id` are foreign keys into `facts(fact_id)`, so deleting
        the fact first raises a foreign-key violation while a relationship
        row still references it. `namespace`, if given, scopes the delete so
        one namespace can't invalidate another's edge by guessing a fact_id.
        """
        with self._lock:
            if namespace is not None:
                cur = self._conn.execute(
                    "DELETE FROM entity_relationships WHERE (source_fact_id = ? OR target_fact_id = ?) "
                    "AND namespace = ?",
                    (fact_id, fact_id, namespace),
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM entity_relationships WHERE source_fact_id = ? OR target_fact_id = ?",
                    (fact_id, fact_id),
                )
            self._maybe_commit()
            return cur.rowcount

    def mark_relationships_stale_for_fact(self, fact_id: int, namespace: str | None = None) -> int:
        """Mark active relationships derived from ``fact_id`` as 'stale'.

        Called alongside `supersede_fact` (by the caller, not automatically)
        so a relationship whose only evidence is a fact that has since been
        corrected stops counting as active evidence, without being
        hard-deleted — the fact itself still exists (as history), so the
        edge might still be true; it just isn't re-asserted by anything
        active anymore. `list_relationships_for_entity`/`relationship_neighbors`
        default to ``status="active"`` and therefore exclude stale rows.
        """
        with self._lock:
            if namespace is not None:
                cur = self._conn.execute(
                    "UPDATE entity_relationships SET status = 'stale', updated_at = CURRENT_TIMESTAMP "
                    "WHERE (source_fact_id = ? OR target_fact_id = ?) AND namespace = ? AND status = 'active'",
                    (fact_id, fact_id, namespace),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE entity_relationships SET status = 'stale', updated_at = CURRENT_TIMESTAMP "
                    "WHERE (source_fact_id = ? OR target_fact_id = ?) AND status = 'active'",
                    (fact_id, fact_id),
                )
            self._maybe_commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Retrieval telemetry (Phase 8 — raw storage primitives; see
    # telemetry.py for the content-free event schema and orchestration).
    # ------------------------------------------------------------------

    def insert_retrieval_event(
        self,
        *,
        event_id: str,
        namespace_digest: str,
        fact_id: int,
        fts_score: float | None,
        jaccard_score: float | None,
        hrr_score: float | None,
        temporal_multiplier: float | None,
        trust_multiplier: float | None,
        final_rank: int,
        algorithm_version: str,
    ) -> None:
        """Insert one content-free retrieval-event row. ``event_id`` is a
        caller-supplied random UUID (never derived from query text)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO retrieval_events
                    (event_id, namespace_digest, fact_id, fts_score, jaccard_score,
                     hrr_score, temporal_multiplier, trust_multiplier, final_rank,
                     helpful, algorithm_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    event_id, namespace_digest or "", int(fact_id),
                    fts_score, jaccard_score, hrr_score,
                    temporal_multiplier, trust_multiplier, int(final_rank),
                    algorithm_version,
                ),
            )
            self._maybe_commit()

    def prune_retrieval_events(self, max_rows: int) -> int:
        """Row-count-cap retention: delete the oldest rows beyond
        ``max_rows`` (ordered by created_at, then rowid as a tiebreak for
        same-timestamp inserts). Returns the number of rows deleted.

        Row-count cap chosen over age-based pruning because it gives a
        deterministic, directly-testable bound ("never more than N rows")
        regardless of insert rate or wall-clock time, which matters for the
        retention test (insert > cap, assert <= cap) required by
        checklist.md section 12.
        """
        if max_rows <= 0:
            return 0
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
            overflow = int(total) - int(max_rows)
            if overflow <= 0:
                return 0
            cur = self._conn.execute(
                """
                DELETE FROM retrieval_events WHERE rowid IN (
                    SELECT rowid FROM retrieval_events
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT ?
                )
                """,
                (overflow,),
            )
            self._maybe_commit()
            return cur.rowcount

    def set_retrieval_feedback(self, event_id: str, helpful: bool) -> bool:
        """Set the helpful/unhelpful flag on exactly one event, by its
        opaque event_id. Returns True iff a row was updated."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE retrieval_events SET helpful = ? WHERE event_id = ?",
                (1 if helpful else 0, event_id),
            )
            self._maybe_commit()
            return cur.rowcount > 0

    def find_latest_pending_retrieval_event(
        self, fact_id: int, namespace_digest: str,
    ) -> str | None:
        """The most recently recorded event for ``(fact_id, namespace_digest)``
        that has no feedback yet, or None.

        Used to link an explicit fact_feedback('helpful'/'unhelpful') call to
        the retrieval event that most plausibly produced it, without ever
        touching an event that already has feedback recorded (so a second,
        unrelated feedback call for the same fact doesn't silently overwrite
        an earlier, already-answered event).
        """
        if not self._table_exists("retrieval_events"):
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT event_id FROM retrieval_events
                WHERE fact_id = ? AND namespace_digest = ? AND helpful IS NULL
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (int(fact_id), namespace_digest or ""),
            ).fetchone()
            return str(row["event_id"]) if row else None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        return dict(row)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
