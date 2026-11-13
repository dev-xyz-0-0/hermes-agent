"""Phase 1 — benchmark library tests.

See docs/plans/demo-reference-plan/checklist.md section 2 ("Benchmark
tests") and implementation-plan.md section 9.7.
"""

from __future__ import annotations

import json

import pytest

from plugins.memory.holographic import benchmark
from plugins.memory.holographic.store import MemoryStore

GOLD_SET_PATH = "tests/fixtures/holographic/gold_set.json"


@pytest.fixture
def fixture_db(tmp_path):
    gold_set = benchmark.load_gold_set(GOLD_SET_PATH)
    db_path = tmp_path / "benchmark.db"
    store = benchmark.build_fixture_db(gold_set, db_path, hrr_dim=32)
    store.close()
    return db_path


class TestGoldSetLoading:
    def test_loads_the_checked_in_gold_set(self):
        gold_set = benchmark.load_gold_set(GOLD_SET_PATH)
        assert len(gold_set.corpus) == 5
        assert len(gold_set.cases) == 5

    def test_missing_file_raises_benchmark_error(self, tmp_path):
        with pytest.raises(benchmark.BenchmarkError):
            benchmark.load_gold_set(tmp_path / "does_not_exist.json")

    def test_invalid_json_raises_benchmark_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(benchmark.BenchmarkError):
            benchmark.load_gold_set(path)

    def test_missing_cases_raises_benchmark_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"corpus": []}), encoding="utf-8")
        with pytest.raises(benchmark.BenchmarkError):
            benchmark.load_gold_set(path)

    def test_case_missing_expected_fact_id_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"corpus": [], "cases": [{"query": "x"}]}), encoding="utf-8")
        with pytest.raises(benchmark.BenchmarkError):
            benchmark.load_gold_set(path)


class TestRunBenchmark:
    def test_metrics_match_hand_calculated_corpus(self, fixture_db):
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        # Every gold-set query is an unambiguous keyword match for its
        # expected fact, so a healthy retriever should get perfect recall.
        assert report["recall_at_1"] == 1.0
        assert report["recall_at_5"] == 1.0
        assert report["mean_reciprocal_rank"] == 1.0
        assert report["empty_result_rate"] == 0.0
        assert report["query_error_rate"] == 0.0
        assert report["namespace_leakage_count"] == 0

    def test_json_output_has_documented_fields(self, fixture_db):
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        serialized = json.dumps(report)  # must be JSON-serializable
        parsed = json.loads(serialized)
        for key in (
            "algorithm_version", "schema_version", "database", "gold_set",
            "numpy_available", "top_k", "query_count", "recall_at_1",
            "recall_at_5", "mean_reciprocal_rank", "empty_result_rate",
            "query_error_rate", "namespace_leakage_count", "latency_ms",
            "strategy_breakdown", "temporal_decay",
        ):
            assert key in parsed

    def test_repeated_runs_are_deterministic(self, fixture_db):
        first = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        second = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        for key in ("recall_at_1", "recall_at_5", "mean_reciprocal_rank", "namespace_leakage_count"):
            assert first[key] == second[key]

    def test_active_eligible_superseded_excluded(self, tmp_path):
        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        active_id = store.add_fact("The current answer is forty two.", category="general")
        store.close()

        store = MemoryStore(db_path=tmp_path / "db.sqlite", hrr_dim=32)
        new_id = store.supersede_fact(active_id, "The corrected answer is forty three.", category="general")
        store.close()

        gold_set_path = tmp_path / "gold.json"
        gold_set_path.write_text(json.dumps({
            "corpus": [],
            "cases": [{"query": "current answer forty", "expected_fact_id": active_id}],
        }), encoding="utf-8")
        report = benchmark.run_benchmark(tmp_path / "db.sqlite", gold_set_path, hrr_dim=32)
        # The superseded fact (active_id) must not be returned at all.
        assert report["recall_at_5"] == 0.0
        assert new_id != active_id

    def test_does_not_increment_retrieval_counters(self, fixture_db):
        store = MemoryStore(db_path=fixture_db, hrr_dim=32)
        before = {
            row["fact_id"]: row["retrieval_count"]
            for row in store._conn.execute("SELECT fact_id, retrieval_count FROM facts").fetchall()
        }
        store.close()

        benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)

        store = MemoryStore(db_path=fixture_db, hrr_dim=32)
        after = {
            row["fact_id"]: row["retrieval_count"]
            for row in store._conn.execute("SELECT fact_id, retrieval_count FROM facts").fetchall()
        }
        store.close()
        assert before == after

    def test_does_not_change_trust_or_feedback(self, fixture_db):
        store = MemoryStore(db_path=fixture_db, hrr_dim=32)
        before = {
            row["fact_id"]: (row["trust_score"], row["helpful_count"])
            for row in store._conn.execute("SELECT fact_id, trust_score, helpful_count FROM facts").fetchall()
        }
        store.close()

        benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)

        store = MemoryStore(db_path=fixture_db, hrr_dim=32)
        after = {
            row["fact_id"]: (row["trust_score"], row["helpful_count"])
            for row in store._conn.execute("SELECT fact_id, trust_score, helpful_count FROM facts").fetchall()
        }
        store.close()
        assert before == after

    def test_numpy_capability_is_reported_accurately(self, fixture_db):
        from plugins.memory.holographic import holographic as hrr
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        assert report["numpy_available"] == hrr._HAS_NUMPY

    def test_strategy_breakdown_present_for_fts_jaccard_hrr(self, fixture_db):
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        for strategy in ("fts_only", "jaccard_only", "hrr_only", "default"):
            assert strategy in report["strategy_breakdown"]
            assert "recall_at_5" in report["strategy_breakdown"][strategy]

    def test_temporal_decay_variants_present(self, fixture_db):
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        assert set(report["temporal_decay"].keys()) == {"disabled", "enabled"}

    def test_latency_percentiles_present_and_non_negative(self, fixture_db):
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        assert report["latency_ms"]["p50"] >= 0.0
        assert report["latency_ms"]["p95"] >= report["latency_ms"]["p50"]

    def test_database_not_found_raises_benchmark_error(self, tmp_path):
        with pytest.raises(benchmark.BenchmarkError):
            benchmark.run_benchmark(tmp_path / "missing.db", GOLD_SET_PATH)


class TestNamespaceLeakageDetector:
    def test_detects_a_deliberately_broken_fixture(self):
        namespace_by_fact_id = {1: "tenant_a", 2: "tenant_b", 3: "tenant_a"}
        # Simulate a broken retrieval path that ignored the namespace filter
        # and returned a fact belonging to a different tenant.
        leaks = benchmark.count_namespace_leakage([1, 2, 3], "tenant_a", namespace_by_fact_id)
        assert leaks == 1  # only fact 2 belongs to a different namespace

    def test_zero_leakage_for_a_correctly_scoped_fixture(self):
        namespace_by_fact_id = {1: "tenant_a", 2: "tenant_a"}
        leaks = benchmark.count_namespace_leakage([1, 2], "tenant_a", namespace_by_fact_id)
        assert leaks == 0

    def test_run_benchmark_reports_zero_leakage_for_unscoped_fixture(self, fixture_db):
        report = benchmark.run_benchmark(fixture_db, GOLD_SET_PATH, hrr_dim=32)
        assert report["namespace_leakage_count"] == 0
