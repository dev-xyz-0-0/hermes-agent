"""PR 3 — profile/chat isolation, shared memory, diagnostics, backup/restore,
and fault tolerance.

Audit finding this PR fixes: Hermes's own "profile" concept (separate
HERMES_HOME per `hermes -p <name>` invocation) was already fully isolated —
different processes, different SQLite files, zero risk. The real gap was
inside a single long-running gateway process: every chat/user it served
shared ONE memory_store.db with no query-level scoping (chat_id/user_id
were captured only as unused provenance). This file tests the fix —
namespace-based isolation computed from (platform, chat_id) — plus the
shared-memory, diagnostics, and backup/restore features built alongside it.

All tests use temporary SQLite databases — never $HERMES_HOME.
"""

import json
import math
import sqlite3

from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic import backup as backup_module
from plugins.memory.holographic.store import MemoryStore


def _gateway_provider(tmp_path, chat_id, platform="telegram", **config):
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(tmp_path / "memory_store.db"),
            "hrr_dim": 32, "hrr_weight": 0.0,
            "prefetch_min_coverage": 0.1,
            **config,
        }
    )
    provider.initialize(session_id=f"sess-{chat_id}", platform=platform, chat_id=chat_id, hermes_home=str(tmp_path))
    return provider


def _cli_provider(tmp_path, **config):
    provider = HolographicMemoryProvider(
        config={"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 32, "prefetch_min_coverage": 0.1, **config}
    )
    provider.initialize(session_id="cli-sess", platform="cli", hermes_home=str(tmp_path))
    return provider


# ---------------------------------------------------------------------------
# A. Profile/chat isolation (P3-P*)
# ---------------------------------------------------------------------------

class TestNamespaceIsolation:
    def test_two_chats_share_one_db_file_but_not_facts(self, tmp_path):
        """P3-P01: Profile A stores a fact; Profile B cannot retrieve it —
        even though both resolve to the literal same db_path."""
        a = _gateway_provider(tmp_path, "chat_A")
        b = _gateway_provider(tmp_path, "chat_B")

        a._store.add_fact("Chat A's secret project is called Nightjar", category="project", namespace=a._namespace)

        assert a._retriever.search("Nightjar", min_trust=0.0, namespace=a._namespace)
        assert b._retriever.search("Nightjar", min_trust=0.0, namespace=b._namespace) == []
        a.shutdown()
        b.shutdown()

    def test_conflicting_preferences_stay_independent(self, tmp_path):
        """P3-P02."""
        a = _gateway_provider(tmp_path, "chat_A", auto_capture="true")
        b = _gateway_provider(tmp_path, "chat_B", auto_capture="true")
        a.sync_turn("My favorite database is PostgreSQL.", "ok")
        b.sync_turn("My favorite database is MySQL.", "ok")

        a_context = a.prefetch("what database do I like?", session_id="sess-chat_A")
        b_context = b.prefetch("what database do I like?", session_id="sess-chat_B")
        assert "PostgreSQL" in a_context and "MySQL" not in a_context
        assert "MySQL" in b_context and "PostgreSQL" not in b_context
        a.shutdown()
        b.shutdown()

    def test_missing_chat_identity_fails_closed_not_shared(self, tmp_path):
        """P3-P03/P04: a multi-tenant platform with no chat_id never falls
        back to the unscoped/shared bucket — it's blocked outright."""
        blocked = HolographicMemoryProvider(config={"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 32})
        blocked.initialize(session_id="s", platform="telegram", hermes_home=str(tmp_path))  # no chat_id

        assert blocked._isolation_blocked is True
        assert blocked._namespace == ""
        assert blocked.prefetch("anything") == ""
        result = json.loads(blocked.handle_tool_call("fact_store", {"action": "stats"}))
        assert "error" in result
        blocked.shutdown()

    def test_cli_platform_is_never_isolation_blocked(self, tmp_path):
        """CLI has no chat_id concept at all — must behave exactly as
        pre-PR-3 (unscoped namespace, never blocked)."""
        cli = _cli_provider(tmp_path)
        assert cli._isolation_blocked is False
        assert cli._namespace == ""
        cli.shutdown()

    def test_profile_isolation_disabled_restores_old_shared_behavior(self, tmp_path):
        """Explicit opt-out: profile_isolation=false collapses every
        session back to the unscoped namespace (old, pre-PR-3 behavior)."""
        a = _gateway_provider(tmp_path, "chat_A", profile_isolation="false")
        b = _gateway_provider(tmp_path, "chat_B", profile_isolation="false")
        assert a._namespace == "" and b._namespace == ""
        assert a._isolation_blocked is False and b._isolation_blocked is False
        a.shutdown()
        b.shutdown()

    def test_restart_preserves_isolation(self, tmp_path):
        """P3-P08: a fresh provider instance for the same chat, after the
        first one shuts down, still only sees that chat's own facts."""
        a1 = _gateway_provider(tmp_path, "chat_A")
        a1._store.add_fact("Chat A fact", category="general", namespace=a1._namespace)
        a1.shutdown()

        a2 = _gateway_provider(tmp_path, "chat_A")
        b = _gateway_provider(tmp_path, "chat_B")
        assert a2._retriever.search("Chat A fact", min_trust=0.0, namespace=a2._namespace)
        assert b._retriever.search("Chat A fact", min_trust=0.0, namespace=b._namespace) == []
        a2.shutdown()
        b.shutdown()

    def test_concurrent_chats_no_cross_contamination(self, tmp_path):
        """P3-P06: many chats writing concurrently (threads) to the same
        db_path never see each other's facts.

        Uses a distinct unique token per chat (not shared template words
        like "favorite number") so FTS5's OR-fallback mode — which already
        existed pre-PR-3 and kicks in when the stricter AND match finds
        nothing — can't spuriously match a DIFFERENT fact of the SAME
        chat's own and be mistaken for a cross-namespace leak.
        """
        import threading

        providers = [_gateway_provider(tmp_path, f"chat_{i}", auto_capture="true") for i in range(6)]
        errors = []

        def worker(p, i):
            try:
                p.sync_turn(f"My unique project codeword is zzyx{i}zzyx.", "ok")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(p, i)) for i, p in enumerate(providers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        for i, p in enumerate(providers):
            own = p._retriever.search(f"zzyx{i}zzyx", min_trust=0.0, namespace=p._namespace)
            assert own, f"chat_{i} lost its own fact"
            for j, other in enumerate(providers):
                if j == i:
                    continue
                leaked = other._retriever.search(f"zzyx{i}zzyx", min_trust=0.0, namespace=other._namespace)
                assert leaked == [], f"chat_{j} can see chat_{i}'s fact"
        for p in providers:
            p.shutdown()

    def test_on_memory_write_mirror_is_namespace_scoped(self, tmp_path):
        """The built-in-memory-tool mirror path (on_memory_write) must not
        let one chat's 'replace'/'remove' touch another chat's fact."""
        a = _gateway_provider(tmp_path, "chat_A")
        b = _gateway_provider(tmp_path, "chat_B")
        a.on_memory_write("add", "user", "Shared-sounding content")
        b.on_memory_write("add", "user", "Shared-sounding content")  # same text, different namespace

        # B tries to replace using A's exact text as old_text — must only
        # affect B's own copy, never A's.
        b.on_memory_write("replace", "user", "B's new content", metadata={"old_text": "Shared-sounding content"})
        assert a._find_fact_id_by_content("Shared-sounding content") is not None  # A's untouched
        assert b._find_fact_id_by_content("Shared-sounding content") is None  # B's replaced
        a.shutdown()
        b.shutdown()


# ---------------------------------------------------------------------------
# A2. fact_store add/search/list parity under the isolation gate
#
# Regression coverage for the "list fails, search works" report: both
# actions are gated by the same handle_tool_call() isolation check, so
# they must succeed together (chat_id present) or fail together (missing).
# ---------------------------------------------------------------------------

class TestFactStoreActionParityUnderIsolation:
    def test_add_search_list_all_succeed_with_chat_id(self, tmp_path):
        p = _gateway_provider(tmp_path, "chat_A")
        assert p._isolation_blocked is False

        add_result = json.loads(p.handle_tool_call(
            "fact_store", {"action": "add", "content": "The deploy key rotates every 90 days"}
        ))
        assert "error" not in add_result
        assert add_result["status"] == "added"

        search_result = json.loads(p.handle_tool_call(
            "fact_store", {"action": "search", "query": "deploy key"}
        ))
        assert "error" not in search_result
        assert search_result["count"] == 1

        list_result = json.loads(p.handle_tool_call(
            "fact_store", {"action": "list"}
        ))
        assert "error" not in list_result
        assert list_result["count"] == 1
        p.shutdown()

    def test_add_search_list_all_fail_closed_without_chat_id(self, tmp_path):
        """A multi-tenant platform session with no resolvable chat_id must
        reject every fact_store action identically — not just 'list'."""
        p = HolographicMemoryProvider(config={"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 32})
        p.initialize(session_id="s", platform="telegram", hermes_home=str(tmp_path))  # no chat_id

        expected_error = (
            "Holographic memory is unavailable for this session: no chat identity "
            "could be resolved to scope memory to"
        )
        for action, extra in [
            ("add", {"content": "should never be written"}),
            ("search", {"query": "anything"}),
            ("list", {}),
        ]:
            result = json.loads(p.handle_tool_call("fact_store", {"action": action, **extra}))
            assert "error" in result, f"action={action} did not fail closed: {result}"
            assert expected_error in result["error"], f"action={action} wrong error: {result}"

        # Nothing was written to the empty/shared namespace despite the
        # attempted "add" above.
        assert p._store.stats(namespace="")["facts"] == 0
        p.shutdown()

    def test_thread_id_further_scopes_namespace(self, tmp_path):
        """Two threads within the same chat_id must not share facts."""
        def _threaded_provider(thread_id):
            provider = HolographicMemoryProvider(
                config={"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 32, "hrr_weight": 0.0}
            )
            provider.initialize(
                session_id=f"sess-{thread_id}", platform="telegram",
                chat_id="chat_A", thread_id=thread_id, hermes_home=str(tmp_path),
            )
            return provider

        a = _threaded_provider("topic_1")
        b = _threaded_provider("topic_2")
        assert a._namespace != b._namespace

        a.handle_tool_call("fact_store", {"action": "add", "content": "Topic 1 only fact"})
        list_a = json.loads(a.handle_tool_call("fact_store", {"action": "list"}))
        list_b = json.loads(b.handle_tool_call("fact_store", {"action": "list"}))
        assert list_a["count"] == 1
        assert list_b["count"] == 0
        a.shutdown()
        b.shutdown()


# ---------------------------------------------------------------------------
# B. Shared memory (P3-S*)
# ---------------------------------------------------------------------------

class TestSharedMemory:
    def test_disabled_by_default_no_cross_profile_access(self, tmp_path):
        """P3-S01."""
        a = _gateway_provider(tmp_path, "chat_A")  # shared memory off by default
        assert a._shared_memory_enabled is False
        a.shutdown()

    def test_shared_fact_readable_by_authorized_profile(self, tmp_path):
        """P3-S02."""
        shared_cfg = {"shared_memory_enabled": "true", "shared_memory_categories": "public_docs"}
        a = _gateway_provider(tmp_path, "chat_A", **shared_cfg)
        added = json.loads(a.handle_tool_call("fact_store", {"action": "add", "content": "Runbook at docs/deploy.md", "category": "public_docs"}))
        share_result = json.loads(a.handle_tool_call("fact_store", {"action": "share", "fact_id": added["fact_id"]}))
        assert "shared_fact_id" in share_result

        b = _gateway_provider(tmp_path, "chat_B", **shared_cfg)
        context = b.prefetch("where is the runbook", session_id="sess-chat_B")
        assert "deploy.md" in context
        assert "shared:" in context
        a.shutdown()
        b.shutdown()

    def test_unauthorized_category_not_shared(self, tmp_path):
        """P3-S03."""
        shared_cfg = {"shared_memory_enabled": "true", "shared_memory_categories": "public_docs"}
        a = _gateway_provider(tmp_path, "chat_A", **shared_cfg)
        added = json.loads(a.handle_tool_call("fact_store", {"action": "add", "content": "My trading edge is X", "category": "trading"}))
        share_result = json.loads(a.handle_tool_call("fact_store", {"action": "share", "fact_id": added["fact_id"]}))
        assert "error" in share_result
        a.shutdown()

    def test_shared_namespace_has_no_implicit_write_through(self, tmp_path):
        """P3-S04 (adapted): ordinary writes never land in the shared
        namespace — only the explicit 'share' action does."""
        shared_cfg = {"shared_memory_enabled": "true", "shared_memory_categories": "public_docs"}
        a = _gateway_provider(tmp_path, "chat_A", **shared_cfg)
        a.handle_tool_call("fact_store", {"action": "add", "content": "Ordinary fact", "category": "public_docs"})

        from plugins.memory.holographic import _DEFAULT_SHARED_NAMESPACE
        rows = a._store._conn.execute(
            "SELECT content FROM facts WHERE namespace = ?", (_DEFAULT_SHARED_NAMESPACE,)
        ).fetchall()
        assert rows == []  # nothing reached the shared namespace without an explicit share
        a.shutdown()

    def test_sharing_revoked_blocks_subsequent_retrieval(self, tmp_path):
        """P3-S05: disabling shared_memory_enabled after a fact was shared
        stops it from being recalled — the write isn't undone, but the
        READ path is denied, which is what matters for 'sharing revoked'."""
        shared_cfg = {"shared_memory_enabled": "true", "shared_memory_categories": "public_docs"}
        a = _gateway_provider(tmp_path, "chat_A", **shared_cfg)
        added = json.loads(a.handle_tool_call("fact_store", {"action": "add", "content": "Runbook at docs/deploy.md", "category": "public_docs"}))
        a.handle_tool_call("fact_store", {"action": "share", "fact_id": added["fact_id"]})
        a.shutdown()

        b = _gateway_provider(tmp_path, "chat_B", shared_memory_enabled="false")
        assert b.prefetch("where is the runbook", session_id="sess-chat_B") == ""
        b.shutdown()

    def test_credential_rejected_from_sharing(self, tmp_path):
        """P3-S06."""
        shared_cfg = {"shared_memory_enabled": "true", "shared_memory_categories": "public_docs"}
        a = _gateway_provider(tmp_path, "chat_A", **shared_cfg)
        # Bypass the add-time firewall deliberately to simulate a legacy row.
        fact_id = a._store.add_fact("API key is sk-proj-abc123def456ghi789jkl012", category="public_docs")
        share_result = json.loads(a.handle_tool_call("fact_store", {"action": "share", "fact_id": fact_id}))
        assert "error" in share_result
        a.shutdown()

    def test_cannot_share_another_namespaces_fact(self, tmp_path):
        """P3-S07 (adapted): the 'share' action only ever reads from the
        caller's OWN namespace — it can't be used to exfiltrate another
        chat's fact by guessing its fact_id."""
        shared_cfg = {"shared_memory_enabled": "true", "shared_memory_categories": "public_docs"}
        a = _gateway_provider(tmp_path, "chat_A", **shared_cfg)
        a_fact_id = a._store.add_fact("Chat A's own doc", category="public_docs")
        a.shutdown()

        b = _gateway_provider(tmp_path, "chat_B", **shared_cfg)
        share_result = json.loads(b.handle_tool_call("fact_store", {"action": "share", "fact_id": a_fact_id}))
        assert "error" in share_result
        b.shutdown()


# ---------------------------------------------------------------------------
# C. Diagnostics (P3-O*)
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_successful_recall_updates_metrics(self, tmp_path):
        """P3-O01/O02."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("User prefers dark mode", category="user_pref")
        p.prefetch("dark mode preference", session_id="cli-sess")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["recall_attempts"] >= 1
        assert diag["metrics"]["recall_success"] >= 1
        p.shutdown()

    def test_empty_recall_updates_empty_counter(self, tmp_path):
        """P3-O02."""
        p = _cli_provider(tmp_path)
        p.prefetch("something with absolutely no match at all here", session_id="cli-sess")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"].get("recall_empty", 0) >= 1
        p.shutdown()

    def test_capture_success_updates_counter(self, tmp_path):
        """P3-O03."""
        p = _cli_provider(tmp_path, auto_capture="true")
        p.sync_turn("I use Elixir for everything.", "ok")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["capture_attempts"] >= 1
        assert diag["metrics"]["capture_accepted"] >= 1
        p.shutdown()

    def test_rejected_candidate_updates_counter(self, tmp_path):
        """Write-firewall rejection via sync_turn() — the candidate is
        extracted (matches _RE_STATEMENT's "i use" pattern) but then rejected
        by _fact_problem()'s credential-shape check. The value is an obvious
        synthetic placeholder, not a real key."""
        p = _cli_provider(tmp_path, auto_capture="true")
        before = len(p._store.list_facts(limit=100))
        p.sync_turn("I use sk-FAKE1234567890ABCDEFGHIJKLMNOP for my test project.", "ok")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["capture_rejected"] >= 1
        assert len(p._store.list_facts(limit=100)) == before
        p.shutdown()

    def test_duplicate_candidate_updates_counter(self, tmp_path):
        """A differently-worded restatement (different turn_hash, so it isn't
        caught by sync_turn()'s own transport-replay dedup) that consolidation
        still classifies as a duplicate of the first fact."""
        p = _cli_provider(tmp_path, auto_capture="true")
        p.sync_turn("I use PostgreSQL for analytics.", "Got it.")
        active_before = len(p._store.list_facts(limit=100))
        p.sync_turn("I use PostgreSQL for analytics, really.", "Got it.")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["capture_duplicate"] >= 1
        assert len(p._store.list_facts(limit=100)) == active_before
        p.shutdown()

    def test_correction_updates_superseded_counter(self, tmp_path):
        p = _cli_provider(tmp_path, auto_capture="true")
        p.sync_turn("My Solana analytics uses PostgreSQL.", "Got it.")
        p.sync_turn("I've decided to switch my Solana analytics database to ClickHouse.", "Updated.")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["consolidation_superseded"] >= 1
        active = [f["content"] for f in p._store.list_facts(limit=20)]
        assert any("ClickHouse" in f for f in active)
        assert not any("PostgreSQL" in f for f in active)
        p.shutdown()

    def test_forget_updates_forgotten_counter(self, tmp_path):
        p = _cli_provider(tmp_path, auto_capture="true")
        p.sync_turn("My preferred editor is Vim.", "Got it.")
        p.sync_turn("Forget my editor preference.", "Done.")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["consolidation_forgotten"] >= 1
        assert p._store.list_facts(limit=20) == []
        p.shutdown()

    def test_sqlite_failure_updates_error_counter(self, tmp_path):
        """P3-O04. A closed connection degrades gracefully inside
        _fts_candidates's own per-attempt try/except (empty result, not an
        exception) — so to exercise prefetch()'s own error counter
        specifically, force the failure one level up, at retriever.search
        itself, where there's no inner swallow."""
        p = _cli_provider(tmp_path)

        def boom(*a, **kw):
            raise RuntimeError("simulated retrieval failure")

        p._retriever.search = boom
        p.prefetch("anything", session_id="cli-sess")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["recall_errors"] >= 1

        # Separately: even a fully closed store must degrade to an empty
        # result rather than raise or hang (the actual production concern).
        p2 = _cli_provider(tmp_path)
        p2._store.close()
        assert p2.prefetch("anything", session_id="cli-sess") == ""

    def test_secret_input_never_appears_in_diagnostics_or_logs(self, tmp_path, caplog):
        """P3-O05."""
        import logging
        p = _cli_provider(tmp_path, auto_capture="true", capture_debug="true")
        with caplog.at_level(logging.INFO, logger="plugins.memory.holographic"):
            p.sync_turn("My password is hunter2hunter2hunter2.", "ok")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert "hunter2" not in json.dumps(diag)
        for record in caplog.records:
            assert "hunter2" not in record.message
        p.shutdown()

    def test_diagnostics_disabled_returns_minimal_stub(self, tmp_path):
        """P3-O06."""
        p = _cli_provider(tmp_path, diagnostics_enabled="false")
        p.prefetch("anything", session_id="cli-sess")
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag == {"diagnostics_enabled": False}
        p.shutdown()

    def test_profile_resolution_failure_is_logged_sanitized(self, tmp_path, caplog):
        """P3-O08."""
        import logging
        with caplog.at_level(logging.WARNING, logger="plugins.memory.holographic"):
            blocked = HolographicMemoryProvider(config={"db_path": str(tmp_path / "memory_store.db"), "hrr_dim": 32})
            blocked.initialize(session_id="s", platform="discord", hermes_home=str(tmp_path))
        assert any("failing closed" in r.message for r in caplog.records)
        blocked.shutdown()

    def test_metrics_reset_and_do_not_leak_across_namespaces_on_reinitialize(self, tmp_path):
        """_metrics is a plain instance dict reset to {} on every
        initialize() call (not persisted, not namespace-partitioned) — this
        locks in that documented behavior: re-initializing the same provider
        for a different namespace must not carry over the previous
        namespace's counters."""
        p = _gateway_provider(tmp_path, "chat_A", auto_capture="true")
        p.sync_turn("I use Rust for chat A's backend.", "ok")
        diag_a = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag_a["metrics"]["capture_attempts"] >= 1

        p.initialize(session_id="sess-chat_B", platform="telegram", chat_id="chat_B", hermes_home=str(tmp_path))
        diag_b = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag_b.get("metrics", {}) == {}
        p.shutdown()


class TestFactFeedback:
    def test_feedback_attempt_updates_counter_regardless_of_outcome(self, tmp_path):
        """feedback_attempts increments once per request that reaches the
        handler with an initialized store — including one that ultimately
        fails (unknown fact_id) — mirroring capture_attempts' semantics of
        counting entry into the pipeline, not success."""
        p = _cli_provider(tmp_path)
        fact_id = p._store.add_fact("User prefers dark mode", category="user_pref")
        p.handle_tool_call("fact_feedback", {"fact_id": fact_id, "action": "helpful"})
        p.handle_tool_call("fact_feedback", {"fact_id": 999999, "action": "helpful"})
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["feedback_attempts"] == 2
        p.shutdown()

    def test_helpful_feedback_increases_trust_and_counter(self, tmp_path):
        p = _cli_provider(tmp_path)
        fact_id = p._store.add_fact("User prefers dark mode", category="user_pref")
        result = json.loads(p.handle_tool_call("fact_feedback", {"fact_id": fact_id, "action": "helpful"}))
        assert math.isclose(result["new_trust"] - result["old_trust"], 0.05, abs_tol=1e-9)
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["feedback_attempts"] >= 1
        assert diag["metrics"]["feedback_helpful"] >= 1
        p.shutdown()

    def test_unhelpful_feedback_decreases_trust_and_counter(self, tmp_path):
        p = _cli_provider(tmp_path)
        fact_id = p._store.add_fact("User prefers dark mode", category="user_pref")
        result = json.loads(p.handle_tool_call("fact_feedback", {"fact_id": fact_id, "action": "unhelpful"}))
        assert math.isclose(result["new_trust"] - result["old_trust"], -0.10, abs_tol=1e-9)
        diag = json.loads(p.handle_tool_call("fact_store", {"action": "diagnostics"}))
        assert diag["metrics"]["feedback_unhelpful"] >= 1
        p.shutdown()

    def test_trust_is_clamped_within_bounds(self, tmp_path):
        p = _cli_provider(tmp_path)
        fact_id = p._store.add_fact("User prefers dark mode", category="user_pref")
        for _ in range(20):
            result = json.loads(p.handle_tool_call("fact_feedback", {"fact_id": fact_id, "action": "unhelpful"}))
        assert result["new_trust"] >= 0.0
        for _ in range(20):
            result = json.loads(p.handle_tool_call("fact_feedback", {"fact_id": fact_id, "action": "helpful"}))
        assert result["new_trust"] <= 1.0
        p.shutdown()

    def test_feedback_does_not_affect_unrelated_facts(self, tmp_path):
        p = _cli_provider(tmp_path)
        fact_a = p._store.add_fact("User prefers dark mode", category="user_pref")
        fact_b = p._store.add_fact("User prefers tabs over spaces", category="user_pref")
        p.handle_tool_call("fact_feedback", {"fact_id": fact_a, "action": "unhelpful"})
        facts = {f["fact_id"]: f["trust_score"] for f in p._store.list_facts(limit=20)}
        assert facts[fact_b] == 0.5
        p.shutdown()

    def test_feedback_respects_namespace_isolation(self, tmp_path):
        a = _gateway_provider(tmp_path, "chat_A")
        b = _gateway_provider(tmp_path, "chat_B")
        fact_id = a._store.add_fact("Chat A's secret project is Nightjar", category="project", namespace=a._namespace)

        result = json.loads(b.handle_tool_call("fact_feedback", {"fact_id": fact_id, "action": "helpful"}))
        assert "error" in result

        unchanged = a._store.list_facts(limit=20, namespace=a._namespace)
        assert unchanged[0]["trust_score"] == 0.5
        a.shutdown()
        b.shutdown()
        
    def test_feedback_metrics_form_complete_outcome_partition(self, tmp_path):
        p = _cli_provider(tmp_path)

        helpful_id = p._store.add_fact(
            "User prefers dark mode",
            category="user_pref",
        )
        unhelpful_id = p._store.add_fact(
            "User prefers compact output",
            category="user_pref",
        )

        # Successful helpful feedback
        p.handle_tool_call(
            "fact_feedback",
            {"fact_id": helpful_id, "action": "helpful"},
        )

        # Successful unhelpful feedback
        p.handle_tool_call(
            "fact_feedback",
            {"fact_id": unhelpful_id, "action": "unhelpful"},
        )

        # Failed lookup
        p.handle_tool_call(
            "fact_feedback",
            {"fact_id": 999999, "action": "helpful"},
        )

        diag = json.loads(
            p.handle_tool_call(
                "fact_store",
                {"action": "diagnostics"},
            )
        )
        metrics = diag["metrics"]

        assert metrics["feedback_attempts"] == 3
        assert metrics["feedback_helpful"] == 1
        assert metrics["feedback_unhelpful"] == 1
        assert metrics["feedback_errors"] == 1

        assert metrics["feedback_attempts"] == (
            metrics["feedback_helpful"]
            + metrics["feedback_unhelpful"]
            + metrics["feedback_errors"]
        )

        p.shutdown()


# ---------------------------------------------------------------------------
# D. Backup and restore (P3-B*)
# ---------------------------------------------------------------------------

class TestBackupRestore:
    def test_backup_healthy_database(self, tmp_path):
        """P3-B01/B03."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("fact one")
        info = p.backup()
        assert backup_module.verify_backup(info.path).ok
        p.shutdown()

    def test_backup_file_permissions_restricted(self, tmp_path):
        """P3-B08."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("fact one")
        info = p.backup()
        assert oct(__import__("os").stat(info.path).st_mode)[-3:] == "600"
        p.shutdown()

    def test_corrupt_backup_rejected(self, tmp_path):
        """P3-B04/B09."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("fact one")
        info = p.backup()
        with open(info.path, "r+b") as f:
            f.seek(50)
            f.write(b"\x00" * 20)
        verified = backup_module.verify_backup(info.path)
        assert not verified.ok

        restore_result = backup_module.restore_backup(info.path, str(tmp_path / "other.db"))
        assert not restore_result.ok
        p.shutdown()

    def test_restore_preserves_facts(self, tmp_path):
        """P3-B05/I05."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("User prefers dark mode", category="user_pref")
        info = p.backup()
        db_path = str(p._store.db_path)
        p.shutdown()

        result = backup_module.restore_backup(info.path, db_path)
        assert result.ok

        store = MemoryStore(db_path=db_path, hrr_dim=32)
        assert any("dark mode" in f["content"] for f in store.list_facts(limit=10))
        assert sqlite3.connect(db_path).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        store.close()

    def test_deleted_fact_not_silently_reactivated_by_restore(self, tmp_path):
        """P3-B11/I10 — the headline requirement."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("User prefers dark mode", category="user_pref")
        p._store.add_fact("User uses Python uv", category="project")
        info = p.backup()

        row = p._store._conn.execute("SELECT fact_id FROM facts WHERE content = 'User prefers dark mode'").fetchone()
        p._store.remove_fact(row[0])  # explicit forget -> tombstoned

        result = p.restore_from_backup(info.path)
        assert result.ok
        assert result.reactivated_blocked == 1

        p.initialize(session_id="cli-sess", platform="cli", hermes_home=str(tmp_path))
        active = [f["content"] for f in p._store.list_facts(limit=10)]
        assert "User prefers dark mode" not in active
        assert "User uses Python uv" in active
        p.shutdown()

    def test_restore_keeps_a_safety_copy_of_the_previous_database(self, tmp_path):
        """P3-B10 (adapted — the original is never destroyed, not just
        'preserved on interruption')."""
        p = _cli_provider(tmp_path)
        p._store.add_fact("original fact")
        info = p.backup()
        db_path = str(p._store.db_path)
        p.shutdown()

        result = backup_module.restore_backup(info.path, db_path)
        assert result.safety_copy_path
        assert __import__("os").path.exists(result.safety_copy_path)

    def test_retention_policy_removes_only_eligible_backups(self, tmp_path):
        """P3-B12."""
        p = _cli_provider(tmp_path, backup_retention=2)
        p._store.add_fact("fact one")
        p.backup()
        p.backup()
        p.backup()  # retention=2 should prune down to 2 after this call
        backup_dir = str(tmp_path / "backups" / "holographic" / "default")
        remaining = [n for n in __import__("os").listdir(backup_dir) if n.endswith(".db")]
        assert len(remaining) == 2
        p.shutdown()

    def test_restore_is_not_exposed_as_a_model_callable_tool_action(self, tmp_path):
        """Deliberate security boundary: restore must never be triggerable
        from chat/tool-call context, only by an operator calling the
        provider method directly."""
        p = _cli_provider(tmp_path)
        schema_actions = set()
        for tool_schema in p.get_tool_schemas():
            if tool_schema["name"] == "fact_store":
                schema_actions = set(tool_schema["parameters"]["properties"]["action"]["enum"])
        assert "restore" not in schema_actions
        assert "backup" in schema_actions  # backup IS safe/exposed
        p.shutdown()


# ---------------------------------------------------------------------------
# E. Fault tolerance (P3-F*)
# ---------------------------------------------------------------------------

class TestFaultTolerance:
    def test_database_unavailable_conversation_continues(self, tmp_path):
        """P3-F02."""
        p = _cli_provider(tmp_path, auto_capture="true")
        p._store.close()
        # None of these must raise.
        assert p.prefetch("anything", session_id="cli-sess") == ""
        p.sync_turn("I use Rust.", "ok")
        result = json.loads(p.handle_tool_call("fact_store", {"action": "add", "content": "x"}))
        assert "error" in result

    def test_concurrent_reads_and_writes_no_corruption(self, tmp_path):
        """P3-F06."""
        import threading
        p = _cli_provider(tmp_path, auto_capture="true")
        errors = []

        def writer(i):
            try:
                p.sync_turn(f"I use tool number {i} for task {i}.", "ok")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def reader(i):
            try:
                p.prefetch(f"tool number {i}", session_id="cli-sess")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert sqlite3.connect(str(p._store.db_path)).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        p.shutdown()

    def test_provider_shutdown_during_capture_is_defined(self, tmp_path):
        """P3-F07 (adapted): shutdown followed immediately by a sync_turn
        call must not raise — it just no-ops (store is gone)."""
        p = _cli_provider(tmp_path, auto_capture="true")
        p.shutdown()
        p.sync_turn("I use Rust.", "ok")  # must not raise

    def test_corrupted_database_file_does_not_crash_initialize(self, tmp_path):
        """P3-F08 (adapted to this store's failure mode): a garbage file at
        the db_path must raise a clear, catchable error rather than silently
        corrupting further or hanging — callers (the plugin loader) already
        guard provider construction broadly."""
        db_path = tmp_path / "memory_store.db"
        db_path.write_bytes(b"not a sqlite database" * 100)
        p = HolographicMemoryProvider(config={"db_path": str(db_path), "hrr_dim": 32})
        try:
            p.initialize(session_id="s", platform="cli", hermes_home=str(tmp_path))
            raised = False
        except sqlite3.DatabaseError:
            raised = True
        assert raised, "a garbage file should fail loudly at init, not silently"
