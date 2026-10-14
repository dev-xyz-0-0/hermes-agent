import json
import sqlite3

import pytest

import plugins.memory.holographic as holographic_plugin
from plugins.memory.holographic import HolographicMemoryProvider


def _provider(tmp_path, **config):
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(tmp_path / "memory_store.db"),
            "hrr_dim": 64,
            "hrr_weight": 0.0,
            **config,
        }
    )
    provider.initialize(
        session_id="foundation-session",
        platform="cli",
        user_id="u1",
        user_name="Tester",
    )
    return provider


def test_provider_imports():
    assert holographic_plugin.HolographicMemoryProvider().name == "holographic"


def test_shutdown_closes_sqlite_connection(tmp_path):
    provider = _provider(tmp_path)
    store = provider._store

    provider.shutdown()

    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")
    assert provider._store is None
    assert provider._retriever is None


def test_mirror_memory_writes_can_be_explicitly_disabled(tmp_path):
    provider = _provider(tmp_path, mirror_memory_writes="false")

    provider.on_memory_write("add", "memory", "Should not be mirrored")

    assert provider._find_fact_id_by_content("Should not be mirrored") is None


def test_auto_extract_string_false_is_false(tmp_path):
    provider = _provider(tmp_path, auto_extract="false")

    provider.on_session_end([{"role": "user", "content": "I prefer dark mode."}])

    assert provider._store.list_facts(limit=100) == []


def test_prefetch_skips_chatter_and_low_coverage(tmp_path):
    provider = _provider(tmp_path, prefetch_min_coverage=0.5)
    provider._store.add_fact("The backend test suite uses pytest", category="project")

    assert provider.prefetch("ok thanks") == ""
    assert provider.prefetch("frontend bundler", session_id="s1") == ""
    assert "pytest" in provider.prefetch("backend pytest", session_id="s1")


def test_prefetch_repeat_suppression(tmp_path):
    provider = _provider(tmp_path, prefetch_min_coverage=0.2, prefetch_repeat_turns=6)
    provider._store.add_fact("Python backend uses pytest", category="project")

    first = provider.prefetch("python backend pytest", session_id="s1")
    second = provider.prefetch("python backend pytest", session_id="s1")

    assert "Python backend uses pytest" in first
    assert second == ""


def test_write_firewall_rejects_secret(tmp_path):
    provider = _provider(tmp_path)

    result = json.loads(
        provider.handle_tool_call(
            "fact_store",
            {"action": "add", "content": "OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"},
        )
    )

    assert "error" in result
    assert provider._store.list_facts(limit=100) == []


def test_recall_firewall_withholds_unsafe_legacy_fact(tmp_path):
    provider = _provider(tmp_path, prefetch_min_coverage=0.1)
    provider._store.add_fact("The OpenAI API key is sk-proj-abc123def456ghi789jkl012", tags="openai api key")

    assert provider.prefetch("openai api key", session_id="s1") == ""


def test_stats_and_provenance_for_tool_write(tmp_path):
    provider = _provider(tmp_path)

    add = json.loads(
        provider.handle_tool_call(
            "fact_store",
            {"action": "add", "content": "Hermes uses pytest for memory tests", "category": "project"},
        )
    )
    stats = json.loads(provider.handle_tool_call("fact_store", {"action": "stats"}))
    source = provider._store.read(
        "SELECT via, platform, session_id, author FROM fact_sources WHERE fact_id = ?",
        (add["fact_id"],),
    )[0]

    assert stats["facts"] == 1
    assert stats["entities"] >= 0
    assert stats["by_category"]["project"] == 1
    assert stats["database"].endswith("memory_store.db")
    assert dict(source) == {
        "via": "tool",
        "platform": "cli",
        "session_id": "foundation-session",
        "author": "Tester",
    }


def test_wal_failure_falls_back(monkeypatch, tmp_path):
    real_connect = sqlite3.connect

    class ConnectionWrapper:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            return setattr(self._conn, name, value)

        def execute(self, sql, *args, **kwargs):
            if str(sql).upper().startswith("PRAGMA JOURNAL_MODE=WAL"):
                raise sqlite3.DatabaseError("wal unavailable")
            return self._conn.execute(sql, *args, **kwargs)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: ConnectionWrapper(real_connect(*args, **kwargs)),
    )

    provider = _provider(tmp_path)

    assert provider._store.count_facts() == 0
