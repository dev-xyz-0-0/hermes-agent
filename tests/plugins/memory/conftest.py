"""Phase 0 baseline fixtures for the Holographic demo-reference integration.

See docs/plans/demo-reference-plan/implementation-plan.md ("Phase 0") and
docs/plans/demo-reference-plan/checklist.md section 1. These fixtures give
every later-phase test (and the Phase 1 benchmark/diagnostics tests) a
reusable, temp-HERMES_HOME-safe way to construct the isolation/schema
scenarios the plan calls for, without re-deriving the setup in every test
module.

All fixtures use ``tmp_path`` for their database files — never the real
``~/.hermes/`` (enforced repo-wide by the autouse ``_isolate_hermes_home``
fixture in tests/conftest.py).
"""

from __future__ import annotations

import sqlite3

import pytest

from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic import holographic as hrr
from plugins.memory.holographic.store import MemoryStore


# ---------------------------------------------------------------------------
# Provider-level isolation fixtures
# ---------------------------------------------------------------------------

def _make_provider(tmp_path, *, platform: str, chat_id: str = "", thread_id: str = "", **config) -> HolographicMemoryProvider:
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(tmp_path / "memory_store.db"),
            "hrr_dim": 32,
            "prefetch_min_coverage": 0.1,
            **config,
        }
    )
    kwargs = {"session_id": f"sess-{platform}-{chat_id or 'cli'}", "platform": platform, "hermes_home": str(tmp_path)}
    if chat_id:
        kwargs["chat_id"] = chat_id
    if thread_id:
        kwargs["thread_id"] = thread_id
    provider.initialize(**kwargs)
    return provider


@pytest.fixture
def unscoped_cli_provider(tmp_path):
    """Phase 0 fixture: unscoped CLI memory (namespace == "", never blocked)."""
    provider = _make_provider(tmp_path, platform="cli")
    yield provider
    provider.shutdown()


@pytest.fixture
def two_gateway_namespaces(tmp_path):
    """Phase 0 fixture: two isolated gateway chats sharing one db file."""
    chat_a = _make_provider(tmp_path, platform="telegram", chat_id="chat_A")
    chat_b = _make_provider(tmp_path, platform="telegram", chat_id="chat_B")
    yield chat_a, chat_b
    chat_a.shutdown()
    chat_b.shutdown()


@pytest.fixture
def threaded_chat_namespace(tmp_path):
    """Phase 0 fixture: a threaded-chat namespace (platform:chat_id:thread_id)."""
    provider = _make_provider(tmp_path, platform="discord", chat_id="guild_1", thread_id="thread_42")
    yield provider
    provider.shutdown()


@pytest.fixture
def shared_memory_namespace(tmp_path):
    """Phase 0 fixture: a provider with shared memory enabled and a category allowlist."""
    provider = _make_provider(
        tmp_path, platform="slack", chat_id="chat_S",
        shared_memory_enabled="true", shared_memory_categories="project",
    )
    yield provider
    provider.shutdown()


# ---------------------------------------------------------------------------
# NumPy availability fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def numpy_enabled(monkeypatch):
    """Force HRR vector operations on, if numpy is actually installed."""
    if not hrr._HAS_NUMPY:
        pytest.skip("numpy is not installed in this environment")
    yield True


@pytest.fixture
def numpy_disabled(monkeypatch):
    """Force the plugin's no-numpy/FTS-only degraded code path."""
    monkeypatch.setattr(hrr, "_HAS_NUMPY", False)
    yield False


# ---------------------------------------------------------------------------
# Historical schema fixtures (pre-PR2 / pre-PR3)
# ---------------------------------------------------------------------------

_PRE_PR2_SCHEMA = """
CREATE TABLE facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX idx_facts_category ON facts(category);
CREATE INDEX idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE facts_fts USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags);
END;
CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.fact_id, old.content, old.tags);
END;
CREATE TRIGGER facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _seed_pre_pr2_db(db_path) -> None:
    """A database as it looked before PR 2 (no status/provenance columns,
    no fact_sources/forgotten_facts tables, global UNIQUE(content))."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PRE_PR2_SCHEMA)
        conn.execute(
            "INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)",
            ("Legacy fact from before PR 2", "general", "", 0.5),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def pre_pr2_db(tmp_path):
    """Phase 0 fixture: a pre-PR2 database file, not yet opened by MemoryStore."""
    db_path = tmp_path / "pre_pr2.db"
    _seed_pre_pr2_db(db_path)
    return db_path


def _seed_pre_pr3_db(db_path) -> None:
    """A database with PR 2's columns (status, superseded_by, source_type,
    extraction_method, confidence) already ALTERed in, but still missing
    `namespace` and still globally UNIQUE on content alone — the exact
    state MemoryStore._migrate_drop_global_content_unique() is designed to
    repair."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PRE_PR2_SCHEMA)
        for col, ddl in (
            ("status", "ALTER TABLE facts ADD COLUMN status TEXT DEFAULT 'active'"),
            ("superseded_by", "ALTER TABLE facts ADD COLUMN superseded_by INTEGER"),
            ("source_type", "ALTER TABLE facts ADD COLUMN source_type TEXT DEFAULT ''"),
            ("extraction_method", "ALTER TABLE facts ADD COLUMN extraction_method TEXT DEFAULT ''"),
            ("confidence", "ALTER TABLE facts ADD COLUMN confidence REAL DEFAULT 1.0"),
        ):
            conn.execute(ddl)
        conn.execute(
            "INSERT INTO facts (content, category, tags, trust_score, status, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Legacy fact from before PR 3 (global unique content)", "general", "", 0.5, "active", 1.0),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def pre_pr3_db(tmp_path):
    """Phase 0 fixture: a pre-PR3 database with a global UNIQUE(content)
    index and PR2 columns, but no `namespace` column yet."""
    db_path = tmp_path / "pre_pr3.db"
    _seed_pre_pr3_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Current-schema fixture with active/superseded/forgotten facts
# ---------------------------------------------------------------------------

@pytest.fixture
def current_schema_mixed_status_db(tmp_path):
    """Phase 0 fixture: a fully current-schema database containing one
    active fact, one superseded fact (with its replacement), and one
    forgotten (tombstoned) fact."""
    db_path = tmp_path / "current_mixed.db"
    store = MemoryStore(db_path=db_path, hrr_dim=32)
    try:
        active_id = store.add_fact("An active fact that nothing has replaced.", category="general")
        old_id = store.add_fact("The old value before a correction.", category="project")
        store.supersede_fact(old_id, "The corrected, current value.", category="project")
        forget_id = store.add_fact("A fact the user later asked to forget.", category="general")
        store.remove_fact(forget_id, tombstone=True)
        yield db_path, {"active_id": active_id, "superseded_id": old_id}
    finally:
        store.close()
