"""Phase 8 — Retrieval telemetry tests.

See docs/plans/demo-reference-plan/implementation-plan.md ("Phase 8 —
Retrieval telemetry and future weight learning") and checklist.md section 12
("Phase 8 — Retrieval telemetry" / "Telemetry tests").

All tests use temporary SQLite databases under `tmp_path` — never
`$HERMES_HOME` (enforced repo-wide by the autouse `_isolate_hermes_home`
fixture in tests/conftest.py).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from plugins.memory.holographic import HolographicMemoryProvider, telemetry
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.retrieval import FactRetriever
from plugins.memory.holographic.store import MemoryStore


def _gateway_provider(tmp_path, chat_id, platform="telegram", **config):
    provider = HolographicMemoryProvider(
        config={
            "db_path": str(tmp_path / "memory_store.db"),
            "hrr_dim": 32,
            "hrr_weight": 0.0,
            "prefetch_min_coverage": 0.1,
            **config,
        }
    )
    provider.initialize(
        session_id=f"sess-{chat_id}", platform=platform, chat_id=chat_id, hermes_home=str(tmp_path),
    )
    return provider


def _row_dicts(db_path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM retrieval_events").fetchall()]
    finally:
        conn.close()


class TestEventSchemaIsContentFree:
    def test_no_raw_namespace_fact_content_or_entity_name_in_any_column(self, tmp_path):
        """Seed a fact with secret-shaped content and a named entity, in a
        real (non-digested) chat namespace. Trigger a real retrieval with
        telemetry enabled. No column of any produced row may contain the
        raw namespace string, the fact content, or the entity name — as a
        full value or as a substring."""
        secret_value = "sk-ant-THIS_IS_A_FAKE_SECRET_VALUE_1234567890"
        entity_name = "Gregory Thistlewood"
        content = f"{entity_name} said the API key is {secret_value} for project Nightjar"
        chat_id = "chat_real_identity_998877"

        provider = _gateway_provider(tmp_path, chat_id, retrieval_telemetry="true")
        try:
            raw_namespace = provider._namespace
            assert chat_id in raw_namespace  # sanity: this really is the raw identity

            fact_id = provider._store.add_fact(content, category="project", namespace=raw_namespace)
            results = provider._retriever.search(
                "Nightjar", min_trust=0.0, namespace=raw_namespace,
            )
            assert any(r["fact_id"] == fact_id for r in results)

            rows = _row_dicts(provider._store.db_path)
            assert rows, "expected at least one retrieval_events row"

            forbidden = [raw_namespace, content, entity_name, secret_value, chat_id]
            for row in rows:
                for col, value in row.items():
                    text = str(value)
                    for bad in forbidden:
                        assert bad not in text, f"column {col!r} leaked {bad!r}: {text!r}"

            # The digest IS expected to appear (that's the whole point).
            expected_digest = _namespace_digest(raw_namespace)
            assert any(row["namespace_digest"] == expected_digest for row in rows)
        finally:
            provider.shutdown()

    def test_event_id_is_a_random_uuid_not_derived_from_query(self, tmp_path):
        """event_id must not be a deterministic hash of the query text —
        two separate calls for the SAME query/fact must still produce two
        distinct ids (a content/query hash would collide here), and each id
        must look like a UUID4, not a content hash."""
        import re
        import uuid

        provider = _gateway_provider(tmp_path, "chat_uuid_check", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            provider._store.add_fact("Shared fact about widgets", category="project", namespace=ns)
            provider._retriever.search("widgets", min_trust=0.0, namespace=ns)
            provider._retriever.search("widgets", min_trust=0.0, namespace=ns)

            rows = _row_dicts(provider._store.db_path)
            assert len(rows) == 2
            ids = [r["event_id"] for r in rows]
            assert ids[0] != ids[1]
            uuid_re = re.compile(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I,
            )
            for event_id in ids:
                assert uuid_re.match(event_id), event_id
                uuid.UUID(event_id)  # must parse
        finally:
            provider.shutdown()

    def test_event_contains_component_scores_rank_and_version(self, tmp_path):
        provider = _gateway_provider(tmp_path, "chat_components", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            provider._store.add_fact("A fact about rockets", category="project", namespace=ns)
            provider._retriever.search("rockets", min_trust=0.0, namespace=ns)

            rows = _row_dicts(provider._store.db_path)
            assert len(rows) == 1
            row = rows[0]
            for col in (
                "fts_score", "jaccard_score", "hrr_score",
                "temporal_multiplier", "trust_multiplier",
            ):
                assert row[col] is not None
            assert row["final_rank"] == 1
            assert row["helpful"] is None
            assert row["algorithm_version"] == telemetry.ALGORITHM_VERSION
            assert row["created_at"]
        finally:
            provider.shutdown()


class TestRetentionBound:
    def test_retention_prunes_oldest_rows_beyond_cap(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            cap = 5
            fact_id = store.add_fact("A fact used only for telemetry retention", category="general")
            for i in range(cap + 10):
                telemetry.record_retrieval_event(
                    store,
                    namespace="",
                    results=[{"fact_id": fact_id}],
                    components={fact_id: {
                        "fts_score": 0.1, "jaccard_score": 0.1, "hrr_score": 0.5,
                        "temporal_multiplier": 1.0, "trust_multiplier": 0.5,
                    }},
                    retention=cap,
                )
            count = store._conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
            assert count == cap
        finally:
            store.close()

    def test_prune_old_events_is_directly_callable(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        try:
            fact_id = store.add_fact("Another telemetry-only fact", category="general")
            for _ in range(20):
                telemetry.record_retrieval_event(
                    store, namespace="", results=[{"fact_id": fact_id}], components={},
                    retention=10_000,  # effectively unbounded for this insert loop
                )
            deleted = telemetry.prune_old_events(store, 3)
            assert deleted == 17
            count = store._conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
            assert count == 3
        finally:
            store.close()


class TestProfileIsolation:
    def test_two_profiles_never_cross_write_telemetry(self, tmp_path):
        profile_a = tmp_path / "profile_a"
        profile_b = tmp_path / "profile_b"
        profile_a.mkdir()
        profile_b.mkdir()

        provider_a = _gateway_provider(profile_a, "chat_A", retrieval_telemetry="true")
        provider_b = _gateway_provider(profile_b, "chat_B", retrieval_telemetry="true")
        try:
            ns_a, ns_b = provider_a._namespace, provider_b._namespace
            provider_a._store.add_fact("Profile A's fact about kites", category="project", namespace=ns_a)
            provider_b._store.add_fact("Profile B's fact about kites", category="project", namespace=ns_b)

            provider_a._retriever.search("kites", min_trust=0.0, namespace=ns_a)
            provider_b._retriever.search("kites", min_trust=0.0, namespace=ns_b)
            provider_b._retriever.search("kites", min_trust=0.0, namespace=ns_b)

            rows_a = _row_dicts(provider_a._store.db_path)
            rows_b = _row_dicts(provider_b._store.db_path)
            assert len(rows_a) == 1
            assert len(rows_b) == 2
            # Different physical files entirely — not merely different rows.
            assert provider_a._store.db_path != provider_b._store.db_path
        finally:
            provider_a.shutdown()
            provider_b.shutdown()


class TestDisabledByDefault:
    def test_default_config_has_telemetry_disabled(self):
        provider = HolographicMemoryProvider(config={})
        assert provider._retrieval_telemetry is False
        assert provider._retrieval_telemetry_retention == telemetry.DEFAULT_RETENTION

    def test_disabled_telemetry_writes_nothing(self, tmp_path):
        provider = _gateway_provider(tmp_path, "chat_disabled")  # retrieval_telemetry left unset -> default false
        try:
            assert provider._retrieval_telemetry is False
            ns = provider._namespace
            provider._store.add_fact("A fact nobody should telemeter", category="project", namespace=ns)
            for _ in range(5):
                provider._retriever.search("telemeter", min_trust=0.0, namespace=ns)

            conn = sqlite3.connect(str(provider._store.db_path))
            try:
                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'retrieval_events'"
                ).fetchone()
                if table_exists:
                    count = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
                    assert count == 0
            finally:
                conn.close()
        finally:
            provider.shutdown()


class TestFeedbackLinking:
    def test_feedback_links_to_correct_pending_event_among_several(self, tmp_path):
        """Two different facts each get a pending retrieval event. Rating
        fact #2 as helpful must set feedback on fact #2's event only — never
        on fact #1's, and never ambiguously on both."""
        provider = _gateway_provider(tmp_path, "chat_feedback", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            fact_id_1 = provider._store.add_fact("Alpha fact about turbines", category="project", namespace=ns)
            fact_id_2 = provider._store.add_fact("Beta fact about turbines", category="project", namespace=ns)
            provider._retriever.search("turbines", min_trust=0.0, limit=10, namespace=ns)

            rows_before = _row_dicts(provider._store.db_path)
            assert {r["fact_id"] for r in rows_before} == {fact_id_1, fact_id_2}
            assert all(r["helpful"] is None for r in rows_before)

            result = json.loads(provider._handle_fact_feedback({"action": "helpful", "fact_id": fact_id_2}))
            assert result["fact_id"] == fact_id_2  # existing response contract unchanged

            rows_after = _row_dicts(provider._store.db_path)
            by_fact = {r["fact_id"]: r["helpful"] for r in rows_after}
            assert by_fact[fact_id_2] == 1
            assert by_fact[fact_id_1] is None
        finally:
            provider.shutdown()

    def test_second_feedback_call_does_not_touch_already_answered_event(self, tmp_path):
        provider = _gateway_provider(tmp_path, "chat_feedback_2", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            fact_id = provider._store.add_fact("Gamma fact about pulleys", category="project", namespace=ns)
            provider._retriever.search("pulleys", min_trust=0.0, namespace=ns)
            provider._handle_fact_feedback({"action": "helpful", "fact_id": fact_id})

            rows = _row_dicts(provider._store.db_path)
            assert len(rows) == 1
            answered_event_id = rows[0]["event_id"]
            assert rows[0]["helpful"] == 1

            # A later retrieval produces a second, still-pending event for
            # the same fact; feedback must land on that one, not re-touch
            # the already-answered first event.
            provider._retriever.search("pulleys", min_trust=0.0, namespace=ns)
            provider._handle_fact_feedback({"action": "unhelpful", "fact_id": fact_id})

            rows = _row_dicts(provider._store.db_path)
            assert len(rows) == 2
            by_id = {r["event_id"]: r["helpful"] for r in rows}
            assert by_id[answered_event_id] == 1  # untouched
            other_id = next(eid for eid in by_id if eid != answered_event_id)
            assert by_id[other_id] == 0
        finally:
            provider.shutdown()

    def test_feedback_response_shape_unchanged_when_telemetry_disabled(self, tmp_path):
        """fact_feedback's own response contract (Phase 0 baseline-compat)
        must be identical whether telemetry is on or off."""
        provider = _gateway_provider(tmp_path, "chat_feedback_off")  # telemetry default off
        try:
            ns = provider._namespace
            fact_id = provider._store.add_fact("Delta fact about levers", category="project", namespace=ns)
            result = json.loads(provider._handle_fact_feedback({"action": "helpful", "fact_id": fact_id}))
            assert set(result.keys()) == {"fact_id", "old_trust", "new_trust", "helpful_count"}
        finally:
            provider.shutdown()


class TestTelemetryFailuresNeverBreakRetrieval:
    def test_search_result_unaffected_when_telemetry_write_raises(self, tmp_path, monkeypatch):
        provider = _gateway_provider(tmp_path, "chat_fail", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            fact_id = provider._store.add_fact("A fact about failing gracefully", category="project", namespace=ns)

            def _boom(*args, **kwargs):
                raise RuntimeError("simulated telemetry backend failure")

            monkeypatch.setattr(telemetry, "record_retrieval_event", _boom)

            # Must not raise, and must return the exact same results a
            # successful telemetry write would have produced.
            results = provider._retriever.search("failing", min_trust=0.0, namespace=ns)
            assert any(r["fact_id"] == fact_id for r in results)

            # And genuinely wrote nothing, since the write itself always raised.
            rows = _row_dicts(provider._store.db_path)
            assert rows == []
        finally:
            provider.shutdown()

    def test_identical_results_with_and_without_telemetry_failure(self, tmp_path, monkeypatch):
        """Same store/query, telemetry enabled in both cases — one run with
        a working telemetry backend, one with a forced failure. The
        `search()` return value must be identical either way."""
        provider = _gateway_provider(tmp_path, "chat_compare", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            provider._store.add_fact("A fact about comparing results", category="project", namespace=ns)

            baseline = provider._retriever.search("comparing", min_trust=0.0, namespace=ns)

            monkeypatch.setattr(
                telemetry, "record_retrieval_event",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
            )
            with_failure = provider._retriever.search("comparing", min_trust=0.0, namespace=ns)

            # Compare everything except `retrieval_count`, which the search
            # path itself increments as a side effect on every call (see
            # FactRetriever.search) and is therefore expected to differ
            # between the two calls regardless of telemetry success/failure.
            def _stable(results):
                return [{k: v for k, v in r.items() if k != "retrieval_count"} for r in results]

            assert _stable(baseline) == _stable(with_failure)
        finally:
            provider.shutdown()

    def test_probe_path_unaffected_by_telemetry_failure_via_search_fallback(self, tmp_path, monkeypatch):
        """probe() without numpy falls back to search() internally — confirm
        that fallback path also survives a telemetry failure."""
        provider = _gateway_provider(tmp_path, "chat_probe_fail", retrieval_telemetry="true", hrr_weight=0.0)
        try:
            ns = provider._namespace
            fact_id = provider._store.add_fact("Entity Zeta appears in this fact", category="project", namespace=ns)

            from plugins.memory.holographic import holographic as hrr_module
            monkeypatch.setattr(hrr_module, "_HAS_NUMPY", False)

            def _boom(*args, **kwargs):
                raise RuntimeError("simulated telemetry backend failure")

            monkeypatch.setattr(telemetry, "record_retrieval_event", _boom)

            results = provider._retriever.probe("Zeta", namespace=ns)
            assert any(r["fact_id"] == fact_id for r in results)
        finally:
            provider.shutdown()


class TestNoWeightMutation:
    def test_telemetry_module_never_writes_to_retriever_weight_attributes(self):
        """Static guard: telemetry.py must not reference/mutate any of
        FactRetriever's scoring-weight attributes. This phase is
        observation-only by design (implementation-plan.md Phase 8); a
        future offline-learning design is explicitly deferred."""
        import inspect

        source = inspect.getsource(telemetry)
        for attr in ("fts_weight", "jaccard_weight", "hrr_weight", "half_life"):
            assert attr not in source

    def test_recording_telemetry_does_not_change_retriever_weights(self, tmp_path):
        provider = _gateway_provider(tmp_path, "chat_weights", retrieval_telemetry="true")
        try:
            ns = provider._namespace
            retriever = provider._retriever
            before = (retriever.fts_weight, retriever.jaccard_weight, retriever.hrr_weight, retriever.half_life)
            provider._store.add_fact("A fact used to check weight stability", category="project", namespace=ns)
            for _ in range(5):
                retriever.search("stability", min_trust=0.0, namespace=ns)
            after = (retriever.fts_weight, retriever.jaccard_weight, retriever.hrr_weight, retriever.half_life)
            assert before == after
        finally:
            provider.shutdown()
