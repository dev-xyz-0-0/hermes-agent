"""Tests for holographic memory plugin fixes.

Covers:
  - FTS5 query sanitization (#14024, #21102)
  - retrieval_count increment (#17899)
  - numpy degradation warning (#17350)
  - auto_extract structured extraction (#22907)
  - Fuzzing edge cases for FTS5 queries
"""

import json
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


@pytest.fixture
def holographic_provider_factory(tmp_path):
    """Create initialized providers sharing only this test's temporary DB."""
    from plugins.memory.holographic import HolographicMemoryProvider

    providers = []
    db_path = str(tmp_path / "lifecycle_memory.db")

    def create(auto_extract=True, *, chat_id="test-chat-a"):
        provider = HolographicMemoryProvider(config={
            "auto_extract": auto_extract,
            "db_path": db_path,
            "hrr_dim": 128,
            "hrr_weight": 0.0,
        })
        provider.initialize(
            f"session-{len(providers) + 1}",
            hermes_home=str(tmp_path),
            platform="telegram",
            chat_id=chat_id,
        )
        providers.append(provider)
        return provider

    yield create

    for provider in providers:
        provider.shutdown()


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
        provider._namespace = ""
        provider._record_source = MagicMock()
        return provider

    @staticmethod
    def _assert_fact(store, content, category):
        facts = store.list_facts()
        assert len(facts) == 1
        assert facts[0]["content"] == content
        assert facts[0]["category"] == category

    def test_preference_extraction(self, store):
        """'I prefer dark mode' should extract 'prefers dark mode'."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "I prefer PostgreSQL for analytics workloads."}])
        self._assert_fact(store, "Prefers PostgreSQL for analytics workloads.", "user_pref")

    def test_preference_comparison(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "I prefer dark mode over light mode."}])
        self._assert_fact(store, "Prefers dark mode over light mode.", "user_pref")

    def test_preference_excludes_trailing_request(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "I prefer dark mode over light mode, can you help me configure it?"}])
        self._assert_fact(store, "Prefers dark mode over light mode.", "user_pref")

    def test_no_raw_message_dump(self, store):
        """'I like that, sounds good' should NOT be stored as a fact."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I like that, sounds good"}
        ])
        assert store.list_facts() == []

    def test_decision_extraction(self, store):
        """'We decided to use PostgreSQL' should extract 'decided to use PostgreSQL'."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "We decided to use CommonJS for this Hermes MCP project."}])
        self._assert_fact(store, "Decided to use CommonJS for this Hermes MCP project.", "project")

    def test_decision_excludes_trailing_question(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "We decided to use PostgreSQL for analytics, but can you first check whether SQLite would be enough?"}])
        self._assert_fact(store, "Decided to use PostgreSQL for analytics.", "project")

    def test_project_requirement(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "The project requires explicit runtime validation."}])
        self._assert_fact(store, "Requires explicit runtime validation.", "project")

    def test_requirement_excludes_followup_command(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "The project requires explicit runtime validation. Can you implement it now?"}])
        self._assert_fact(store, "Requires explicit runtime validation.", "project")

    def test_project_technology(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": "The project uses PostgreSQL."}])
        self._assert_fact(store, "Uses PostgreSQL.", "project")

    def test_habit_extraction(self, store):
        """A stated habit should be normalized to a declarative fact."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {
                "role": "user",
                "content": "I always run git status before committing changes.",
            }
        ])
        self._assert_fact(
            store,
            "Always runs git status before committing changes.",
            "user_pref",
        )

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
            {"role": "user", "content": "I prefer"}
        ])
        facts = store.list_facts()
        assert len(facts) == 0

    @pytest.mark.parametrize("content", [
        "I want you to add tests for the endpoint.",
        "Do I prefer PostgreSQL or SQLite?",
        'You said "I prefer PostgreSQL". Is that correct?',
    ])
    def test_non_fact_user_messages_ignored(self, store, content):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{"role": "user", "content": content}])
        assert store.list_facts() == []

    @pytest.mark.parametrize("role", ["system", "tool", "assistant"])
    def test_non_user_roles_ignored(self, store, role):
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": role, "content": "I prefer structured outputs."}
        ])
        assert store.list_facts() == []

    def test_compaction_summary_ignored(self, store):
        provider = self._make_provider(store)
        provider._auto_extract_facts([{
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION] Earlier turns in this conversation were "
                "compacted to save context space.\nI prefer PostgreSQL."
            ),
        }])
        assert store.list_facts() == []

    def test_one_fact_per_message(self, store):
        """A single message should produce at most one fact."""
        provider = self._make_provider(store)
        provider._auto_extract_facts([
            {"role": "user", "content": "I prefer Rust. We decided to use Rust. I always use Rust."}
        ])
        facts = store.list_facts(category="user_pref")
        # Should produce at most 1 fact for this message (first match wins)
        assert len([f for f in facts if "Rust" in f["content"]]) <= 1
        self._assert_fact(store, "Prefers Rust.", "user_pref")

    def test_fact_length_capped(self, store):
        """Extracted facts should not exceed 200 characters."""
        provider = self._make_provider(store)
        long_pref = "x" * 300
        provider._auto_extract_facts([
            {"role": "user", "content": f"I prefer {long_pref}"}
        ])
        facts = store.list_facts()
        assert len(facts) == 1
        assert facts[0]["category"] == "user_pref"
        assert len(facts[0]["content"]) <= 200


class TestAutoExtractLifecycle:
    """Configuration, lifecycle, namespace, and deduplication regressions."""

    message = [{
        "role": "user",
        "content": "I prefer PostgreSQL for analytics workloads.",
    }]

    @staticmethod
    def _tool(provider, action, **kwargs):
        return json.loads(provider.handle_tool_call(
            "fact_store", {"action": action, **kwargs}
        ))

    def test_auto_extract_false_disables_extraction(
        self, holographic_provider_factory
    ):
        provider = holographic_provider_factory(False)
        provider.on_session_end(self.message)
        assert provider._store.list_facts() == []

        # The setting gates automatic extraction only; explicit storage remains active.
        result = self._tool(
            provider,
            "add",
            content="Explicitly stored fact.",
            category="general",
        )
        assert result["status"] == "added"
        facts = self._tool(provider, "list")["facts"]
        assert [fact["content"] for fact in facts] == ["Explicitly stored fact."]

    @pytest.mark.parametrize("configured_value", ["false", "False", "0"])
    def test_auto_extract_string_false_disables_extraction(
        self, holographic_provider_factory, configured_value
    ):
        provider = holographic_provider_factory(configured_value)
        provider.on_session_end(self.message)
        assert provider._store.list_facts() == []

    @pytest.mark.parametrize("configured_value", [True, "true", "True", "1"])
    def test_auto_extract_string_true_enables_extraction(
        self, holographic_provider_factory, configured_value
    ):
        provider = holographic_provider_factory(configured_value)
        provider.on_session_end(self.message)
        facts = provider._store.list_facts(namespace="telegram:test-chat-a")
        assert len(facts) == 1
        assert facts[0]["content"] == "Prefers PostgreSQL for analytics workloads."
        assert facts[0]["category"] == "user_pref"

    def test_auto_extract_runs_on_real_session_end(
        self, holographic_provider_factory
    ):
        provider = holographic_provider_factory(True)
        provider.on_session_end(self.message)
        facts = self._tool(provider, "list")["facts"]
        assert len(facts) == 1
        assert facts[0]["content"] == "Prefers PostgreSQL for analytics workloads."
        assert facts[0]["category"] == "user_pref"

    def test_auto_extract_disabled_on_real_session_end(
        self, holographic_provider_factory
    ):
        provider = holographic_provider_factory(False)
        provider.on_session_end(self.message)
        assert self._tool(provider, "list")["facts"] == []

    def test_auto_extract_respects_namespace_isolation(
        self, holographic_provider_factory
    ):
        provider_a = holographic_provider_factory(True, chat_id="test-chat-a")
        provider_b = holographic_provider_factory(True, chat_id="test-chat-b")
        provider_a.on_session_end(self.message)
        provider_b.on_session_end([{
            "role": "user",
            "content": "I prefer SQLite for local prototypes.",
        }])

        facts_a = self._tool(provider_a, "list")["facts"]
        facts_b = self._tool(provider_b, "list")["facts"]
        assert [fact["content"] for fact in facts_a] == [
            "Prefers PostgreSQL for analytics workloads."
        ]
        assert [fact["content"] for fact in facts_b] == [
            "Prefers SQLite for local prototypes."
        ]
        assert self._tool(provider_a, "search", query="SQLite")["results"] == []
        assert self._tool(provider_b, "search", query="PostgreSQL")["results"] == []

    def test_auto_extract_same_fact_not_duplicated_same_namespace(
        self, holographic_provider_factory
    ):
        first_session = holographic_provider_factory(True, chat_id="test-chat-a")
        first_session.on_session_end(self.message)
        first_session.shutdown()

        second_session = holographic_provider_factory(True, chat_id="test-chat-a")
        second_session.on_session_end(self.message)
        facts = self._tool(second_session, "list")["facts"]
        assert len(facts) == 1
        assert facts[0]["content"] == "Prefers PostgreSQL for analytics workloads."

    def test_auto_extract_same_fact_allowed_different_namespaces(
        self, holographic_provider_factory
    ):
        provider_a = holographic_provider_factory(True, chat_id="test-chat-a")
        provider_b = holographic_provider_factory(True, chat_id="test-chat-b")
        provider_a.on_session_end(self.message)
        provider_b.on_session_end(self.message)

        facts_a = self._tool(provider_a, "list")["facts"]
        facts_b = self._tool(provider_b, "list")["facts"]
        assert len(facts_a) == 1
        assert len(facts_b) == 1
        assert facts_a[0]["content"] == facts_b[0]["content"]
        assert facts_a[0]["namespace"] == "telegram:test-chat-a"
        assert facts_b[0]["namespace"] == "telegram:test-chat-b"

    def test_auto_extract_similar_but_distinct_facts_preserved(
        self, holographic_provider_factory
    ):
        provider = holographic_provider_factory(True, chat_id="test-chat-a")
        provider.on_session_end(self.message)
        provider.on_session_end([{
            "role": "user",
            "content": "I prefer PostgreSQL for production workloads.",
        }])

        facts = self._tool(provider, "list")["facts"]
        assert {fact["content"] for fact in facts} == {
            "Prefers PostgreSQL for analytics workloads.",
            "Prefers PostgreSQL for production workloads.",
        }


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
