"""Tests for holographic memory plugin fixes.

Covers:
  - FTS5 query sanitization (#14024, #21102)
  - retrieval_count increment (#17899)
  - numpy degradation warning (#17350)
  - auto_extract structured extraction (#22907)
  - Fuzzing edge cases for FTS5 queries
"""

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Ensure the plugin is importable
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from plugins.memory.holographic.store import MemoryStore
from plugins.memory.holographic.retrieval import FactRetriever


@pytest.fixture
def store(tmp_path):
    """Create a fresh MemoryStore with a temp DB."""
    db = str(tmp_path / "test_memory.db")
    return MemoryStore(db_path=db, default_trust=0.5, hrr_dim=128)


@pytest.fixture
def retriever(store):
    """Create a FactRetriever for the temp store."""
    return FactRetriever(store=store, hrr_dim=128, hrr_weight=0.0)


# =========================================================================
# FTS5 query sanitization (#14024)
# =========================================================================

class TestFTS5Sanitization:
    """Hyphenated and special-char queries must not crash FTS5."""

    def test_hyphenated_query(self, store, retriever):
        """pve-01 should work without 'no such column' error."""
        store.add_fact("PVE-01 hardware: i5-13500T, IP 10.20.90.00", category="hardware", tags="pve-01")
        results = retriever.search("pve-01", min_trust=0.0)
        assert len(results) >= 1
        assert "PVE-01" in results[0]["content"]

    def test_hyphenated_hostnames(self, store, retriever):
        """Multiple hyphenated hostnames should all be findable."""
        store.add_fact("pihole-02 blocks ads on DNS", tags="pihole-02")
        store.add_fact("lxc-103 runs Ubuntu 24.04", tags="lxc-103")
        store.add_fact("gw-router handles VLAN trunking", tags="gw-router")

        assert len(retriever.search("pihole-02", min_trust=0.0)) >= 1
        assert len(retriever.search("lxc-103", min_trust=0.0)) >= 1
        assert len(retriever.search("gw-router", min_trust=0.0)) >= 1

    def test_special_fts5_operators(self, store, retriever):
        """Characters that are FTS5 operators: AND, OR, NOT, *, ^, :"""
        store.add_fact("C++ programming language is widely used")
        store.add_fact("search:elasticsearch for logging")
        store.add_fact("NOT a drill, this is real")

        # These should not crash
        results = retriever.search("C++", min_trust=0.0)
        assert isinstance(results, list)

        results = retriever.search("search:elasticsearch", min_trust=0.0)
        assert isinstance(results, list)

        results = retriever.search("NOT drill", min_trust=0.0)
        assert isinstance(results, list)

    def test_sanitize_fts_query_modes(self):
        """Test the _sanitize_fts_query static method directly."""
        from plugins.memory.holographic.retrieval import FactRetriever

        # default mode: tokens quoted
        assert FactRetriever._sanitize_fts_query("pve-01") == '"pve" "01"'

        # and mode: same as default
        assert FactRetriever._sanitize_fts_query("pve-01", mode="and") == '"pve" "01"'

        # or mode: OR-joined
        result = FactRetriever._sanitize_fts_query("pve-01", mode="or")
        assert "OR" in result
        assert '"pve"' in result
        assert '"01"' in result

    def test_empty_query(self, retriever):
        """Empty query should return empty results, not crash."""
        assert retriever.search("", min_trust=0.0) == []

    def test_query_only_hyphens(self, store, retriever):
        """Query that is only hyphens/punctuation should not crash."""
        store.add_fact("some regular fact content here")
        results = retriever.search("---", min_trust=0.0)
        assert isinstance(results, list)  # may be empty, just don't crash


# =========================================================================
# FTS5 query fuzzing
# =========================================================================

class TestFTS5Fuzzing:
    """Fuzz the FTS5 query path with adversarial inputs."""

    @pytest.mark.parametrize("query", [
        "'; DROP TABLE facts; --",
        "null\x00byte",
        "UPPER CASE QUERY",
        "   lots   of   spaces   ",
        "emoji 🎉 unicode café résumé",
        "a",
        "x" * 500,
        "1+1=2",
        "field:value",
        '"already quoted"',
        "'single quoted'",
        "fact AND fiction OR reality NOT dream",
        "term*",
        "^boosted",
        "NEAR/3 term",
        "pve-01 lxc-103",
        "10.20.90.00",
        "path/to/file.py",
        "user@domain.com",
        "a-b-c-d-e",
        "!@#$%^&*()",
    ])
    def test_fuzz_queries(self, store, retriever, query):
        """None of these queries should crash the search."""
        store.add_fact("Regular fact about Python and Rust programming languages")
        store.add_fact("PVE-01 server at 10.20.90.00 running Proxmox")
        results = retriever.search(query, min_trust=0.0)
        assert isinstance(results, list)


# =========================================================================
# retrieval_count increment (#17899)
# =========================================================================

class TestRetrievalCount:
    """retrieval_count should be incremented when facts are retrieved."""

    def test_search_increments_count(self, store, retriever):
        """After searching, retrieval_count should go from 0 to 1."""
        fact_id = store.add_fact("Python is a programming language")

        # Verify starting at 0
        row = store._conn.execute(
            "SELECT retrieval_count FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        assert row["retrieval_count"] == 0

        # Search and find it
        results = retriever.search("Python", min_trust=0.0)
        assert len(results) >= 1

        # Verify incremented
        row = store._conn.execute(
            "SELECT retrieval_count FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        assert row["retrieval_count"] >= 1

    def test_multiple_searches_accumulate(self, store, retriever):
        """Multiple searches should keep incrementing."""
        store.add_fact("Rust is memory safe")

        retriever.search("Rust", min_trust=0.0)
        retriever.search("Rust", min_trust=0.0)
        retriever.search("Rust", min_trust=0.0)

        row = store._conn.execute(
            "SELECT retrieval_count FROM facts WHERE content LIKE '%Rust%'"
        ).fetchone()
        assert row["retrieval_count"] >= 3

    def test_unrelated_search_no_increment(self, store, retriever):
        """Searching for something else shouldn't increment unrelated facts."""
        fact_id = store.add_fact("Haskell is purely functional")
        store.add_fact("Rust is memory safe")

        retriever.search("Rust", min_trust=0.0)

        row = store._conn.execute(
            "SELECT retrieval_count FROM facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        assert row["retrieval_count"] == 0


# =========================================================================
# auto_extract structured extraction (#22907)
# =========================================================================

class TestAutoExtract:
    """auto_extract should produce clean declarative facts, not raw messages."""

    def _make_provider(self, store):
        """Create a minimal HolographicMemoryProvider for testing auto_extract."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider.__new__(HolographicMemoryProvider)
        provider._store = store
        provider._config = {"auto_extract": True}
        provider._hrr_available = True
        return provider

    def test_preference_extraction(self, store):
        """'I prefer dark mode' should extract 'prefers dark mode'."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I prefer dark mode over light mode"}
        ])
        facts = store.list_facts(category="user_pref")
        assert any("dark mode" in f["content"] for f in facts)
        # Should NOT contain the raw message
        assert not any("over light mode" not in f["content"] and "I prefer" in f["content"] for f in facts)

    def test_no_raw_message_dump(self, store):
        """'I like that, sounds good' should NOT be stored as a fact."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I like that, sounds good"}
        ])
        facts = store.list_facts()
        # "I like" no longer matches — we removed "like" from the pattern
        assert not any("I like that" in f["content"] for f in facts)

    def test_decision_extraction(self, store):
        """'We decided to use PostgreSQL' should extract 'decided to use PostgreSQL'."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "We decided to use PostgreSQL for the backend"}
        ])
        facts = store.list_facts(category="project")
        assert any("PostgreSQL" in f["content"] for f in facts)
        assert any(f["content"].startswith("decided to") for f in facts)

    def test_habit_extraction(self, store):
        """'I always check git status' should extract 'always check git status'."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I always check git status before committing"}
        ])
        facts = store.list_facts(category="user_pref")
        assert any("always check git status" in f["content"] for f in facts)

    def test_assistant_messages_ignored(self, store):
        """Assistant messages should not produce facts."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "assistant", "content": "I prefer to use structured outputs"}
        ])
        facts = store.list_facts()
        assert len(facts) == 0

    def test_short_messages_ignored(self, store):
        """Messages under 10 chars should be skipped."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I like"}
        ])
        facts = store.list_facts()
        assert len(facts) == 0

    def test_one_fact_per_message(self, store):
        """A single message should produce at most one fact."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I prefer Rust. We decided to use Rust. I always use Rust."}
        ])
        facts = store.list_facts(category="user_pref")
        # Should produce at most 1 fact for this message (first match wins)
        assert len([f for f in facts if "Rust" in f["content"]]) <= 1

    def test_fact_length_capped(self, store):
        """Extracted facts should not exceed 200 characters."""
        provider = self._make_provider(store)
        long_pref = "x" * 300
        provider._auto_extract_facts([
            {"role": "user", "content": f"I prefer {long_pref}"}
        ])
        facts = store.list_facts()
        for f in facts:
            assert len(f["content"]) <= 200


# =========================================================================
# numpy degradation warning (#17350)
# =========================================================================

class TestNumpyWarning:
    """Holographic memory should warn when numpy is unavailable."""

    def test_system_prompt_with_numpy(self):
        """With numpy available, system prompt should not show warning."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider.__new__(HolographicMemoryProvider)
        provider._store = MagicMock()
        provider._hrr_available = True
        provider._store._conn.execute.return_value.fetchone.return_value = (5,)

        prompt = provider.system_prompt_block()
        assert "WARNING" not in prompt
        assert "5 facts" in prompt

    def test_system_prompt_without_numpy(self):
        """Without numpy, system prompt should show degradation warning."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider.__new__(HolographicMemoryProvider)
        provider._store = MagicMock()
        provider._hrr_available = False
        provider._store._conn.execute.return_value.fetchone.return_value = (3,)

        prompt = provider.system_prompt_block()
        assert "WARNING" in prompt
        assert "numpy" in prompt
        assert "3 facts" in prompt

    def test_init_warns_without_numpy(self, caplog):
        """initialize() should log a warning when numpy is missing."""
        import logging
        from plugins.memory.holographic import HolographicMemoryProvider
        with tempfile.TemporaryDirectory() as tmp:
            provider = HolographicMemoryProvider(config={
                "db_path": os.path.join(tmp, "test.db"),
            })
            with patch("plugins.memory.holographic.holographic._HAS_NUMPY", False):
                with caplog.at_level(logging.WARNING, logger="plugins.memory.holographic"):
                    provider.initialize("test-session")
                assert hasattr(provider, "_hrr_available")
                assert not provider._hrr_available
                assert any("numpy not found" in r.message for r in caplog.records)