"""PR 2 — automatic capture, consolidation, and forgetting.

Covers: deterministic rule-based extraction (capture.py), scope-aware
consolidation (new/duplicate/correction/forget, consolidation.py), the
sync_turn() wiring in the provider (idempotency, firewall, fail-open,
auto_capture/consolidation_enabled flags), the schema migration that adds
status/superseded_by/source_type/extraction_method/confidence to a pre-PR2
database, and an end-to-end MemoryManager-level integration test for the
exact scenario the PR 2 spec walks through.

All tests use temporary SQLite databases — never $HERMES_HOME.
"""

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import wait
from types import SimpleNamespace

from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic import capture, consolidation
from plugins.memory.holographic.store import MemoryStore
from plugins.memory.holographic.retrieval import FactRetriever


def _provider(tmp_path, **config):
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(tmp_path / "memory_store.db"),
            "hrr_dim": 32,
            "hrr_weight": 0.0,
            "auto_capture": "true",
            "prefetch_min_coverage": 0.2,
            **config,
        }
    )
    provider.initialize(session_id="capture-session", platform="cli")
    return provider


# ---------------------------------------------------------------------------
# A. Capture — deterministic extraction (capture.py)
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_statement_extracted(self):
        cands = capture.extract_candidates("I use Python uv for MCP projects.")
        assert len(cands) == 1
        assert cands[0].operation == "statement"
        assert "uv" in cands[0].content

    def test_greeting_produces_no_fact(self):
        assert capture.extract_candidates("Hello, how are you?") == []

    def test_general_knowledge_question_produces_no_fact(self):
        assert capture.extract_candidates("What is PostgreSQL?") == []

    def test_quoted_reported_speech_produces_no_fact(self):
        assert capture.extract_candidates('"I love PostgreSQL", said the article.') == []

    def test_hypothetical_produces_no_fact(self):
        assert capture.extract_candidates("For example, I might use PostgreSQL for a toy project.") == []
        assert capture.extract_candidates("We're evaluating ClickHouse.") == []

    def test_short_chatter_produces_no_fact(self):
        assert capture.extract_candidates("ok thanks") == []

    def test_multiple_durable_facts_in_one_message(self):
        cands = capture.extract_candidates(
            "I use Python uv for MCP projects. My preferred editor is Vim."
        )
        assert len(cands) == 2

    def test_forget_request_is_classified_as_forget_not_statement(self):
        cands = capture.extract_candidates("Forget my editor preference.")
        assert len(cands) == 1
        assert cands[0].operation == "forget"

    def test_dont_remember_this_is_forget_not_captured_as_new_fact(self):
        cands = capture.extract_candidates("Don't remember this information.")
        assert len(cands) == 1
        assert cands[0].operation == "forget"

    def test_correction_phrasing_detected(self):
        cands = capture.extract_candidates(
            "I've decided to switch my Solana analytics database to ClickHouse."
        )
        assert len(cands) == 1
        assert cands[0].operation == "correction"

    def test_max_candidates_is_respected(self):
        text = " ".join(f"I use tool{i} for project{i}." for i in range(10))
        cands = capture.extract_candidates(text, max_candidates=3)
        assert len(cands) == 3

    def test_hybrid_eligibility_rejects_questions(self):
        assert not capture.eligible_for_hybrid("Which dependency manager do I use?")
        assert capture.eligible_for_hybrid("The dependency manager of choice remains uv.")

    def test_model_json_is_strictly_validated(self):
        cands = capture.candidates_from_json(
            '{"candidates":[{"content":"User prefers uv", "operation":"statement", "confidence":2}]}'
        )
        assert cands == []


# ---------------------------------------------------------------------------
# B. Capture — wired into sync_turn() (provider level)
# ---------------------------------------------------------------------------

class TestSyncTurnCapture:
    def test_natural_statement_is_captured_without_fact_store_call(self, tmp_path):
        provider = _provider(tmp_path)
        provider.sync_turn("I'm using Python uv for my MCP projects.", "Got it.")
        facts = [f["content"] for f in provider._store.list_facts(limit=20)]
        assert any("uv" in f for f in facts)

    def test_assistant_authored_text_is_never_considered(self, tmp_path):
        """Capture only ever looks at user_content — an assistant inventing a
        preference in its own reply can't become a fact, structurally."""
        provider = _provider(tmp_path)
        provider.sync_turn("ok", "I prefer dark mode and use PostgreSQL for everything.")
        assert provider._store.list_facts(limit=20) == []

    def test_hybrid_fallback_captures_ambiguous_user_statement(self, tmp_path, monkeypatch):
        seen_messages = []

        def fake_call_llm(**kwargs):
            seen_messages.extend(kwargs["messages"])
            message = SimpleNamespace(content=json.dumps({"candidates": [{
                "content": "User prefers uv for dependency management",
                "operation": "statement",
                "scope": "dependency management",
                "confidence": 0.9,
            }]}))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
        provider = _provider(tmp_path, extraction_mode="hybrid")
        provider.sync_turn(
            "The dependency manager of choice remains uv.",
            "I invented an assistant-only preference for pip.",
        )
        wait(list(provider._capture_futures), timeout=2)
        facts = provider._store.list_facts(limit=20)
        provider.shutdown()

        assert [fact["content"] for fact in facts] == ["User prefers uv for dependency management"]
        assert all("assistant-only" not in message["content"] for message in seen_messages)

    def test_hybrid_never_calls_model_for_question(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kwargs: called.append(kwargs))
        provider = _provider(tmp_path, extraction_mode="hybrid")
        provider.sync_turn("Which dependency manager do I use?", "You use uv.")
        provider.shutdown()
        assert called == []
        assert provider._metrics["capture_zero_candidates"] == 1

    def test_secret_is_never_stored_even_if_syntactically_capturable(self, tmp_path):
        provider = _provider(tmp_path)
        provider.sync_turn("My API key is sk-proj-abc123def456ghi789jkl012.", "Noted.")
        assert provider._store.list_facts(limit=20) == []

    def test_same_turn_processed_twice_is_one_logical_write(self, tmp_path):
        provider = _provider(tmp_path)
        before = len(provider._store.list_facts(limit=100))
        provider.sync_turn("My favorite editor is Helix.", "Cool.")
        provider.sync_turn("My favorite editor is Helix.", "Cool.")  # replay
        after = len(provider._store.list_facts(limit=100))
        assert after - before == 1

    def test_auto_capture_disabled_means_no_automatic_writes(self, tmp_path):
        provider = _provider(tmp_path, auto_capture="false")
        provider.sync_turn("I use Elixir for everything now.", "Cool.")
        assert provider._store.list_facts(limit=20) == []

    def test_auto_capture_disabled_keeps_explicit_fact_store_working(self, tmp_path):
        provider = _provider(tmp_path, auto_capture="false")
        added = json.loads(provider.handle_tool_call("fact_store", {"action": "add", "content": "Explicit fact"}))
        assert added["status"] == "added"

    def test_store_failure_does_not_raise(self, tmp_path):
        provider = _provider(tmp_path)
        provider._store.close()
        provider.sync_turn("I use Rust for backend services.", "Noted.")  # must not raise

    def test_capture_debug_logs_sanitized_diagnostics_only(self, tmp_path, caplog):
        provider = _provider(tmp_path, capture_debug="true")
        with caplog.at_level(logging.INFO, logger="plugins.memory.holographic"):
            provider.sync_turn("My secret project codename is Nightjar.", "Noted.")
        records = [r for r in caplog.records if "holographic.capture" in r.message]
        assert records
        for r in records:
            assert "Nightjar" not in r.message

    def test_capture_timeout_stops_processing_early(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, capture_timeout_ms=10, max_facts_per_turn=10)
        real_monotonic = time.monotonic
        calls = {"n": 0}

        def fake_monotonic():
            calls["n"] += 1
            return real_monotonic() + (10.0 if calls["n"] > 1 else 0.0)

        monkeypatch.setattr("plugins.memory.holographic.time.monotonic", fake_monotonic)
        text = " ".join(f"I use tool{i} for project{i}." for i in range(5))
        provider.sync_turn(text, "ok")  # must not hang or raise


# ---------------------------------------------------------------------------
# C. Consolidation — new / duplicate / correction / conflict
# ---------------------------------------------------------------------------

def _store_and_retriever(tmp_path, name="c.db"):
    store = MemoryStore(db_path=str(tmp_path / name), hrr_dim=32)
    retriever = FactRetriever(store=store, hrr_dim=32, hrr_weight=0.0)
    return store, retriever


def _apply(store, retriever, text, category="project"):
    results = []
    for c in capture.extract_candidates(text):
        results.append(consolidation.classify_and_apply(store, retriever, c, category=category))
    return results


class TestConsolidation:
    def test_exact_duplicate_stays_one_active_fact(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "My MCP projects use Python uv.")
        results = _apply(store, retriever, "My MCP projects use Python uv.")
        assert results[0].action == "ignored"
        assert len(store.list_facts(limit=20)) == 1

    def test_paraphrase_does_not_create_a_redundant_active_fact(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        for text in [
            "My MCP projects use Python uv.",
            "I use Python uv for MCP projects.",
            "I prefer uv for Python MCP development.",
        ]:
            _apply(store, retriever, text)
        assert len(store.list_facts(limit=20)) == 1

    def test_correction_supersedes_old_fact(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "My Solana analytics uses PostgreSQL.")
        _apply(store, retriever, "I've decided to switch my Solana analytics database to ClickHouse.")

        active = [f["content"] for f in store.list_facts(limit=20)]
        assert not any("PostgreSQL" in f for f in active)
        assert any("ClickHouse" in f for f in active)

        history = store.get_history()
        assert any("PostgreSQL" in f["content"] and f["status"] == "superseded" for f in history)

    def test_superseded_fact_not_returned_by_normal_search(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "My Solana analytics uses PostgreSQL.")
        _apply(store, retriever, "I've decided to switch my Solana analytics database to ClickHouse.")
        results = retriever.search("PostgreSQL", min_trust=0.0)
        assert results == []

    def test_different_projects_both_retained(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "Project Alpha uses PostgreSQL.")
        _apply(store, retriever, "Project Beta uses SQLite.")
        active = {f["content"] for f in store.list_facts(limit=20)}
        assert active == {"Project Alpha uses PostgreSQL.", "Project Beta uses SQLite."}

    def test_ambiguous_same_scope_conflict_is_not_silently_overwritten(self, tmp_path):
        """No correction verb -> both statements are preserved, not merged
        and not silently replaced. (See consolidation.py module docstring —
        this is the documented conservative tradeoff.)"""
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "Project Alpha uses PostgreSQL.")
        _apply(store, retriever, "Project Alpha uses MySQL.")
        active = {f["content"] for f in store.list_facts(limit=20)}
        assert "Project Alpha uses PostgreSQL." in active
        assert "Project Alpha uses MySQL." in active

    def test_correction_with_no_matching_prior_fact_inserts_standalone(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        results = _apply(store, retriever, "I've decided to switch my blog host to Netlify.")
        assert results[0].action == "inserted"

    def test_repeated_identical_correction_is_idempotent(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "My Solana analytics uses PostgreSQL.")
        _apply(store, retriever, "I've decided to switch my Solana analytics database to ClickHouse.")
        active_after_first = sorted(f["content"] for f in store.list_facts(limit=20))

        # Re-apply the same correction again — must not blow up or duplicate.
        _apply(store, retriever, "I've decided to switch my Solana analytics database to ClickHouse.")
        active_after_second = sorted(f["content"] for f in store.list_facts(limit=20))
        assert active_after_first == active_after_second

    def test_correction_transaction_failure_leaves_original_fact_intact(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        old_id = store.add_fact("Solana analytics uses PostgreSQL", category="project")

        class FailingConn:
            """Delegates everything to the real connection except the INSERT
            that would create the replacement fact — simulating a write
            failure mid-supersession, the same wrapper-proxy technique used
            in test_holographic_foundation.py::test_wal_failure_falls_back."""

            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args, **kwargs):
                if "INSERT INTO FACTS" in str(sql).upper():
                    raise sqlite3.OperationalError("disk I/O error")
                return self._conn.execute(sql, *args, **kwargs)

        real_conn = store._conn
        store._conn = FailingConn(real_conn)
        try:
            try:
                store.supersede_fact(old_id, "Solana analytics uses ClickHouse", category="project")
            except sqlite3.OperationalError:
                pass
        finally:
            store._conn = real_conn

        row = store._conn.execute(
            "SELECT status, content FROM facts WHERE fact_id = ?", (old_id,)
        ).fetchone()
        assert row["status"] == "active"
        assert row["content"] == "Solana analytics uses PostgreSQL"
        assert len(store.list_facts(limit=20)) == 1

    def test_legacy_fact_without_new_metadata_is_still_searchable(self, tmp_path):
        """A fact added before status/confidence existed (simulated via the
        store's own add_fact, which defaults those columns) must behave
        identically to a freshly captured one."""
        store, retriever = _store_and_retriever(tmp_path)
        store.add_fact("Legacy fact with no provenance", category="general")
        assert len(retriever.search("Legacy fact", min_trust=0.0)) == 1


# ---------------------------------------------------------------------------
# D. Forgetting
# ---------------------------------------------------------------------------

class TestConcurrency:
    """P2-M08 / spec section 11 (I07): concurrent duplicate capture events
    against the SAME store must not corrupt the database, deadlock, or
    leave more than one active fact behind. The store already serializes
    access with threading.RLock + SQLite busy_timeout; this pins that it
    actually holds under contention from multiple threads."""

    def test_concurrent_identical_captures_produce_one_active_fact(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path, name="concurrent.db")
        text = "My MCP projects use Python uv."
        errors = []

        def worker():
            try:
                _apply(store, retriever, text)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert all(not t.is_alive() for t in threads)  # no deadlock
        assert len(store.list_facts(limit=50)) == 1


class TestForgetting:
    def test_forget_known_preference_removes_it_from_recall(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "My preferred editor is Vim.")
        _apply(store, retriever, "Forget my editor preference.")
        assert store.list_facts(limit=20) == []
        assert retriever.search("editor", min_trust=0.0) == []

    def test_forget_nonexistent_fact_is_safe_noop(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        results = _apply(store, retriever, "Forget my shoe size.")
        assert results[0].action == "noop"

    def test_forget_does_not_affect_unrelated_project(self, tmp_path):
        store, retriever = _store_and_retriever(tmp_path)
        _apply(store, retriever, "Project Alpha uses PostgreSQL.")
        _apply(store, retriever, "Project Beta uses SQLite.")
        _apply(store, retriever, "Forget Project Alpha's database choice.")
        active = [f["content"] for f in store.list_facts(limit=20)]
        assert active == ["Project Beta uses SQLite."]

    def test_forget_survives_restart_with_same_db(self, tmp_path):
        db_path = str(tmp_path / "restart.db")
        store1 = MemoryStore(db_path=db_path, hrr_dim=32)
        retriever1 = FactRetriever(store=store1, hrr_dim=32, hrr_weight=0.0)
        _apply(store1, retriever1, "My preferred editor is Vim.")
        _apply(store1, retriever1, "Forget my editor preference.")
        store1.close()

        store2 = MemoryStore(db_path=db_path, hrr_dim=32)
        assert store2.list_facts(limit=20) == []
        store2.close()

    def test_consolidation_disabled_skips_forget_instead_of_guessing(self, tmp_path):
        provider = _provider(tmp_path, consolidation_enabled="false")
        provider._store.add_fact("My preferred editor is Vim.", category="user_pref")
        provider.sync_turn("Forget my editor preference.", "ok")
        # Forgetting requires consolidation to find its target safely — with
        # it disabled, the fact is left untouched rather than guessed at.
        assert len(provider._store.list_facts(limit=20)) == 1


# ---------------------------------------------------------------------------
# E. Schema migration — pre-PR2 database compatibility
# ---------------------------------------------------------------------------

class TestMigration:
    def _make_legacy_db(self, path: str) -> None:
        """Build a facts table matching the schema BEFORE PR 2's columns
        (status/superseded_by/source_type/extraction_method/confidence)."""
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE facts (
                fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'general',
                tags TEXT DEFAULT '',
                trust_score REAL DEFAULT 0.5,
                retrieval_count INTEGER DEFAULT 0,
                helpful_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE entities (
                entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                entity_type TEXT DEFAULT 'unknown',
                aliases TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE fact_entities (
                fact_id INTEGER REFERENCES facts(fact_id),
                entity_id INTEGER REFERENCES entities(entity_id),
                PRIMARY KEY (fact_id, entity_id)
            );
            CREATE VIRTUAL TABLE facts_fts USING fts5(content, tags, content=facts, content_rowid=fact_id);
            CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
                INSERT INTO facts_fts(rowid, content, tags) VALUES (new.fact_id, new.content, new.tags);
            END;
        """)
        conn.execute("INSERT INTO facts (content, category) VALUES ('Legacy fact predates PR2', 'general')")
        conn.commit()
        conn.close()

    def test_legacy_database_migrates_without_data_loss(self, tmp_path):
        db_path = str(tmp_path / "legacy.db")
        self._make_legacy_db(db_path)

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        facts = store.list_facts(limit=20)
        assert len(facts) == 1
        assert facts[0]["content"] == "Legacy fact predates PR2"
        assert facts[0]["status"] == "active"  # backfilled by the migration
        store.close()

    def test_migration_is_idempotent(self, tmp_path):
        db_path = str(tmp_path / "legacy2.db")
        self._make_legacy_db(db_path)

        store1 = MemoryStore(db_path=db_path, hrr_dim=32)
        store1.close()
        # Re-opening (migration runs again) must not error or duplicate columns.
        store2 = MemoryStore(db_path=db_path, hrr_dim=32)
        assert len(store2.list_facts(limit=20)) == 1
        store2.close()

    def test_new_facts_can_be_captured_after_migration(self, tmp_path):
        db_path = str(tmp_path / "legacy3.db")
        self._make_legacy_db(db_path)

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        new_id = store.add_fact("Fresh fact after migration", category="general")
        assert new_id is not None
        assert len(store.list_facts(limit=20)) == 2
        store.close()


# ---------------------------------------------------------------------------
# F. End-to-end via MemoryManager — the PR 2 spec's worked scenario
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def _manager(self, tmp_path, **config):
        from agent.memory_manager import MemoryManager
        from agent.builtin_memory_provider import BuiltinMemoryProvider

        mgr = MemoryManager()
        mgr.add_provider(BuiltinMemoryProvider())
        provider = HolographicMemoryProvider(
            config={
                "db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 32,
                "prefetch_min_coverage": 0.2, "auto_capture": "true", **config,
            }
        )
        mgr.add_provider(provider)
        mgr.initialize_all(session_id="cli-session", platform="cli")
        return mgr, provider

    def test_natural_capture_then_cross_session_recall_no_fact_store_call(self, tmp_path):
        mgr, provider = self._manager(tmp_path)

        # "Conversation 1": natural statement, captured via sync_turn (the
        # real post-turn hook — same call site run_agent.py uses).
        mgr.sync_all("I'm using Python uv for my MCP projects.", "Got it.")
        mgr.shutdown_all()

        # "Conversation 2": a brand new provider instance, same DB — recall
        # happens through PR 1's prefetch, no fact_store call anywhere.
        mgr2, provider2 = self._manager(tmp_path)
        context = mgr2.prefetch_all("What package manager do I use for MCP development?")
        assert "uv" in context
        mgr2.shutdown_all()

    def test_correction_flows_through_to_recall(self, tmp_path):
        mgr, provider = self._manager(tmp_path)
        mgr.sync_all("My Solana analytics uses PostgreSQL.", "Got it.")
        mgr.sync_all("I've decided to switch my Solana analytics database to ClickHouse.", "Updated.")
        mgr.shutdown_all()

        mgr2, _ = self._manager(tmp_path)
        context = mgr2.prefetch_all("What database am I using for Solana analytics?")
        assert "ClickHouse" in context
        assert "PostgreSQL" not in context
        mgr2.shutdown_all()

    def test_duplicate_statements_across_five_turns_stay_one_fact(self, tmp_path):
        mgr, provider = self._manager(tmp_path)
        for _ in range(5):
            mgr.sync_all("My MCP projects use Python uv.", "Noted.")
        assert len(provider._store.list_facts(limit=20)) == 1
        mgr.shutdown_all()
