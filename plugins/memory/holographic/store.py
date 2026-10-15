"""
SQLite-backed fact store with entity resolution and trust scoring.
Single-user Hermes memory store plugin.
"""

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
        self._init_db()

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
        self._conn.executescript(_SCHEMA)
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
        """
        unique_on_content_alone = False
        for idx in self._conn.execute("PRAGMA index_list(facts)").fetchall():
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in self._conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
            if cols == ["content"]:
                unique_on_content_alone = True
                break
        if not unique_on_content_alone:
            return

        fk_was_on = bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("ALTER TABLE facts RENAME TO facts_pre_ns_unique")
            self._conn.execute("ALTER TABLE fact_entities RENAME TO fact_entities_pre_ns_unique")
            self._conn.execute("ALTER TABLE fact_sources RENAME TO fact_sources_pre_ns_unique")
            # RENAME rewrites trigger/FK bodies to point at the renamed table
            # (verified empirically) — same as the FK issue above. Drop the
            # triggers explicitly so _SCHEMA's CREATE TRIGGER IF NOT EXISTS
            # below actually (re)creates them bound to the NEW `facts`,
            # instead of silently no-op'ing against the misbound old name.
            self._conn.execute("DROP TRIGGER IF EXISTS facts_ai")
            self._conn.execute("DROP TRIGGER IF EXISTS facts_ad")
            self._conn.execute("DROP TRIGGER IF EXISTS facts_au")

            self._conn.executescript(_SCHEMA)  # recreates facts/fact_entities/fact_sources/triggers fresh

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
                self._conn.commit()
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
            self._rebuild_bank(category)

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
                self._conn.commit()

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
            self._conn.commit()

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
                    "SELECT fact_id, trust_score FROM facts WHERE fact_id = ? AND namespace = ?",
                    (fact_id, namespace),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT fact_id, trust_score FROM facts WHERE fact_id = ?", (fact_id,)
                ).fetchone()
            if row is None:
                return False

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
            self._conn.commit()

            # If content changed, re-extract entities
            if content is not None:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,)
                )
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, entity_id)
                self._conn.commit()

            # Recompute HRR vector if content changed
            if content is not None:
                self._compute_hrr_vector(fact_id, content)
            # Rebuild bank for relevant category
            cat = category or self._conn.execute(
                "SELECT category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()["category"]
            self._rebuild_bank(cat)

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
                self._conn.commit()
            except sqlite3.IntegrityError:
                # new_content collides with an existing fact's UNIQUE
                # (content, namespace) — that fact becomes the replacement
                # instead of inserting a duplicate.
                self._conn.rollback()
                existing = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ? AND namespace = ?",
                    (new_content, resolved_namespace),
                ).fetchone()
                if existing is None:
                    raise
                new_fact_id = int(existing["fact_id"])
                if new_fact_id != old_fact_id:
                    self._conn.execute(
                        "UPDATE facts SET status = 'superseded', superseded_by = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                        (new_fact_id, old_fact_id),
                    )
                    self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

            if new_fact_id != old_fact_id:
                for name in self._extract_entities(new_content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(new_fact_id, entity_id)
                self._compute_hrr_vector(new_fact_id, new_content)
                self._rebuild_bank(resolved_category)
                if old_row["category"] != resolved_category:
                    self._rebuild_bank(old_row["category"])

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
            self._conn.commit()
            self._rebuild_bank(row["category"])
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
            self._conn.commit()

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
        self._conn.commit()
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
        self._conn.commit()

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
            self._conn.commit()

    def _rebuild_bank(self, category: str) -> None:
        """Full rebuild of a category's memory bank from all its fact vectors."""
        with self._lock:
            if not self._hrr_available:
                return

            bank_name = f"cat:{category}"
            rows = self._conn.execute(
                "SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL",
                (category,),
            ).fetchall()

            if not rows:
                self._conn.execute("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
                self._conn.commit()
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
                (bank_name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, fact_count),
            )
            self._conn.commit()

    def rebuild_all_vectors(self, dim: int | None = None) -> int:
        """Recompute all HRR vectors + banks from text. For recovery/migration.

        Returns the number of facts processed.
        """
        with self._lock:
            if not self._hrr_available:
                return 0

            if dim is not None:
                self.hrr_dim = dim

            rows = self._conn.execute(
                "SELECT fact_id, content, category FROM facts"
            ).fetchall()

            categories: set[str] = set()
            for row in rows:
                self._compute_hrr_vector(row["fact_id"], row["content"])
                categories.add(row["category"])

            for category in categories:
                self._rebuild_bank(category)

            return len(rows)

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
