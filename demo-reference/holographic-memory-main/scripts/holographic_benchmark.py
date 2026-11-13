#!/usr/bin/env python3
"""Benchmark R@5 retrieval quality for the Hermes holographic memory store."""

from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, "$HERMES_HOME/hermes-agent")

from plugins.memory.holographic.retrieval import FactRetriever  # noqa: E402
from plugins.memory.holographic.store import MemoryStore  # noqa: E402


DB_PATH = Path("~/.hermes/memory_store.db").expanduser()
RESULTS_PATH = Path("~/.hermes/memory_benchmark.json").expanduser()
TARGET_CASES = 100
RANDOM_SEED = 20260516
TOP_K = 5

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "about",
    "when",
    "where",
    "what",
    "which",
    "should",
    "would",
    "could",
    "using",
    "use",
    "not",
    "are",
    "was",
    "were",
    "have",
    "has",
    "had",
    "will",
    "can",
    "your",
    "you",
    "user",
    "用户",
}


@dataclass(frozen=True)
class TestCase:
    case_id: int
    fact_id: int
    query: str
    query_type: str
    category: str
    expected_content: str
    entities: tuple[str, ...]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def clean_query(query: str, fallback: str) -> str:
    query = re.sub(r"[\x00-\x1f]", " ", query)
    query = re.sub(r"[\"'`^:(){}\[\]/\\|*+-]", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        query = fallback
    return query[:180].strip()


def extract_terms(text: str, tags: str = "", max_terms: int = 6) -> list[str]:
    raw = f"{text} {tags}"
    ascii_terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", raw)
    cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,8}", raw)
    mixed_terms = re.findall(r"[A-Za-z0-9_.-]*[\u4e00-\u9fff]+[A-Za-z0-9_.-]*", raw)

    seen: set[str] = set()
    terms: list[str] = []
    for term in ascii_terms + mixed_terms + cjk_terms:
        normalized = term.strip(".,;!?，。；！？、<>")
        key = normalized.lower()
        if len(normalized) < 2 or key in STOPWORDS or key in seen:
            continue
        seen.add(key)
        terms.append(normalized)

    terms.sort(key=lambda t: (len(t), sum(ch.isupper() for ch in t)), reverse=True)
    return terms[:max_terms]


def content_window(content: str, width: int = 64) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= width:
        return compact

    chunks = re.split(r"[。.!?；;，,]", compact)
    chunks = [chunk.strip() for chunk in chunks if len(chunk.strip()) >= 8]
    if chunks:
        best = max(chunks, key=len)
        return best[:width]
    return compact[:width]


def fetch_facts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    entity_clause = ""
    if table_exists(conn, "entities") and table_exists(conn, "fact_entities"):
        entity_clause = """
            , COALESCE((
                SELECT GROUP_CONCAT(e.name, '||')
                FROM fact_entities fe
                JOIN entities e ON e.entity_id = fe.entity_id
                WHERE fe.fact_id = f.fact_id
            ), '') AS entity_names
        """
    else:
        entity_clause = ", '' AS entity_names"

    rows = conn.execute(
        f"""
        SELECT f.fact_id, f.content, f.category, f.tags, f.trust_score,
               f.retrieval_count, f.updated_at
               {entity_clause}
        FROM facts f
        WHERE f.content IS NOT NULL
          AND LENGTH(TRIM(f.content)) >= 8
          AND f.trust_score >= 0.1
        ORDER BY f.category, f.trust_score DESC, f.fact_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def balanced_sample(facts: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        by_category[fact.get("category") or "general"].append(fact)

    rng = random.Random(RANDOM_SEED)
    for rows in by_category.values():
        rng.shuffle(rows)

    categories = sorted(by_category, key=lambda c: len(by_category[c]), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[int] = set()

    while len(selected) < min(target, len(facts)):
        progressed = False
        for category in categories:
            bucket = by_category[category]
            while bucket and int(bucket[-1]["fact_id"]) in seen:
                bucket.pop()
            if not bucket:
                continue
            fact = bucket.pop()
            selected.append(fact)
            seen.add(int(fact["fact_id"]))
            progressed = True
            if len(selected) >= min(target, len(facts)):
                break
        if not progressed:
            break

    return selected


def build_query(fact: dict[str, Any], case_index: int) -> tuple[str, str, tuple[str, ...]]:
    content = str(fact.get("content") or "")
    tags = str(fact.get("tags") or "")
    entities = tuple(
        entity.strip()
        for entity in str(fact.get("entity_names") or "").split("||")
        if entity.strip()
    )
    terms = extract_terms(content, tags)
    fallback = content_window(content, 48)

    variant = case_index % 3
    if variant == 0 and entities:
        query = " ".join(entities[:3])
        query_type = "entity_names"
    elif variant == 1 and terms:
        query = " ".join(terms[:4])
        query_type = "key_terms"
    else:
        query = content_window(content)
        query_type = "partial_content"

    if query_type == "entity_names" and len(query) < 3 and terms:
        query = " ".join(terms[:3])
        query_type = "key_terms"
    if query_type == "key_terms" and len(query) < 3:
        query = fallback
        query_type = "partial_content"

    return clean_query(query, fallback), query_type, entities


def generate_test_cases(conn: sqlite3.Connection, target: int = TARGET_CASES) -> list[TestCase]:
    facts = fetch_facts(conn)
    sample = balanced_sample(facts, target)
    entity_candidates = [fact for fact in facts if str(fact.get("entity_names") or "").strip()]
    if entity_candidates:
        rng = random.Random(RANDOM_SEED + 1)
        rng.shuffle(entity_candidates)
        entity_quota = min(target // 3, len(entity_candidates), target)
        selected_ids = {int(fact["fact_id"]) for fact in sample}
        current_entity_count = sum(1 for fact in sample if str(fact.get("entity_names") or "").strip())

        for candidate in entity_candidates:
            if current_entity_count >= entity_quota:
                break
            if int(candidate["fact_id"]) in selected_ids:
                continue
            replace_index = next(
                (
                    index
                    for index in range(len(sample) - 1, -1, -1)
                    if not str(sample[index].get("entity_names") or "").strip()
                ),
                None,
            )
            if replace_index is None:
                break
            selected_ids.discard(int(sample[replace_index]["fact_id"]))
            sample[replace_index] = candidate
            selected_ids.add(int(candidate["fact_id"]))
            current_entity_count += 1

        entity_sample = [fact for fact in sample if str(fact.get("entity_names") or "").strip()]
        non_entity_sample = [fact for fact in sample if not str(fact.get("entity_names") or "").strip()]
        reordered: list[dict[str, Any]] = []
        entity_iter = iter(entity_sample)
        non_entity_iter = iter(non_entity_sample)
        for index in range(len(sample)):
            if index % 3 == 0:
                try:
                    reordered.append(next(entity_iter))
                    continue
                except StopIteration:
                    pass
            try:
                reordered.append(next(non_entity_iter))
            except StopIteration:
                reordered.append(next(entity_iter))
        sample = reordered
    cases: list[TestCase] = []

    for index, fact in enumerate(sample):
        query, query_type, entities = build_query(fact, index)
        cases.append(
            TestCase(
                case_id=index + 1,
                fact_id=int(fact["fact_id"]),
                query=query,
                query_type=query_type,
                category=str(fact.get("category") or "general"),
                expected_content=str(fact.get("content") or ""),
                entities=entities,
            )
        )

    return cases


def retriever_configs() -> dict[str, dict[str, float]]:
    return {
        "fts5_only": {
            "fts_weight": 1.05,
            "jaccard_weight": 0.0,
            "hrr_weight": 0.05,
            "vector_weight": 0.05,
            "graph_weight": 0.0,
        },
        "jaccard_only": {
            "fts_weight": 0.05,
            "jaccard_weight": 1.0,
            "hrr_weight": 0.05,
            "vector_weight": 0.05,
            "graph_weight": 0.0,
        },
        "hrr_only": {
            "fts_weight": 0.05,
            "jaccard_weight": 0.0,
            "hrr_weight": 1.05,
            "vector_weight": 0.05,
            "graph_weight": 0.0,
        },
        "vector_only": {
            "fts_weight": 0.05,
            "jaccard_weight": 0.0,
            "hrr_weight": 0.05,
            "vector_weight": 1.05,
            "graph_weight": 0.0,
        },
        "graph_only": {
            "fts_weight": 0.05,
            "jaccard_weight": 0.0,
            "hrr_weight": 0.05,
            "vector_weight": 0.05,
            "graph_weight": 1.0,
        },
        "all_5_default": {},
    }


def effective_weights(retriever: FactRetriever) -> dict[str, float]:
    return {
        "fts": float(retriever.fts_weight),
        "jaccard": float(retriever.jaccard_weight),
        "hrr": float(retriever.hrr_weight),
        "vector": float(retriever.vector_weight),
        "graph": float(retriever.graph_weight),
    }


def snapshot_mutable_columns(conn: sqlite3.Connection) -> dict[int, tuple[int, str | None]]:
    rows = conn.execute("SELECT fact_id, retrieval_count, updated_at FROM facts").fetchall()
    return {
        int(row["fact_id"]): (int(row["retrieval_count"] or 0), row["updated_at"])
        for row in rows
    }


def restore_mutable_columns(conn: sqlite3.Connection, snapshot: dict[int, tuple[int, str | None]]) -> None:
    conn.executemany(
        "UPDATE facts SET retrieval_count = ?, updated_at = ? WHERE fact_id = ?",
        [(count, updated_at, fact_id) for fact_id, (count, updated_at) in snapshot.items()],
    )
    conn.commit()


def run_method(
    method_name: str,
    config: dict[str, float],
    store: MemoryStore,
    cases: list[TestCase],
) -> dict[str, Any]:
    retriever = FactRetriever(store=store, **config)
    start = time.perf_counter()
    hits = 0
    failures: list[dict[str, Any]] = []
    category_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"hits": 0, "total": 0})
    query_type_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"hits": 0, "total": 0})
    latencies: list[float] = []

    for case in cases:
        case_start = time.perf_counter()
        error: str | None = None
        try:
            results = retriever.search(case.query, min_trust=0.1, limit=TOP_K)
        except Exception as exc:
            results = []
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - case_start
        latencies.append(elapsed)

        result_ids = [int(result.get("fact_id")) for result in results if result.get("fact_id") is not None]
        hit = case.fact_id in result_ids[:TOP_K]
        hits += int(hit)

        category_stats[case.category]["total"] += 1
        category_stats[case.category]["hits"] += int(hit)
        query_type_stats[case.query_type]["total"] += 1
        query_type_stats[case.query_type]["hits"] += int(hit)

        if not hit:
            failures.append(
                {
                    "case_id": case.case_id,
                    "fact_id": case.fact_id,
                    "query": case.query,
                    "query_type": case.query_type,
                    "category": case.category,
                    "expected_content": case.expected_content[:240],
                    "top5_ids": result_ids[:TOP_K],
                    "top5": [
                        {
                            "fact_id": int(result.get("fact_id")),
                            "score": round(float(result.get("score", 0.0)), 6),
                            "content": str(result.get("content", ""))[:180],
                        }
                        for result in results[:TOP_K]
                    ],
                    "result_count": len(results),
                    "error": error,
                    "latency_ms": round(elapsed * 1000, 3),
                }
            )

    duration = time.perf_counter() - start
    total = len(cases)
    return {
        "method": method_name,
        "weights": effective_weights(retriever),
        "hits": hits,
        "total": total,
        "r_at_5": hits / total if total else 0.0,
        "misses": total - hits,
        "duration_seconds": duration,
        "avg_latency_ms": mean(latencies) * 1000 if latencies else 0.0,
        "category_breakdown": summarize_breakdown(category_stats),
        "query_type_breakdown": summarize_breakdown(query_type_stats),
        "failures": failures,
    }


def summarize_breakdown(stats: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "hits": value["hits"],
            "total": value["total"],
            "r_at_5": value["hits"] / value["total"] if value["total"] else 0.0,
        }
        for key, value in sorted(stats.items())
    }


def format_pct(value: float) -> str:
    return f"{value * 100:6.2f}%"


def print_report(report: dict[str, Any]) -> None:
    print("Hermes Holographic Memory R@5 Benchmark")
    print("=" * 48)
    print(f"Database: {report['database_path']}")
    print(f"Generated at: {report['generated_at']}")
    print(f"Facts in store: {report['facts_total']}")
    print(f"Test cases: {report['test_case_count']}")
    print(f"Results JSON: {report['results_path']}")
    print()

    default = report["methods"].get("all_5_default", {})
    print(f"Overall R@5 (all_5_default): {format_pct(default.get('r_at_5', 0.0))}")
    print()

    print("Per-method R@5")
    print("-" * 96)
    print(f"{'method':<18} {'R@5':>8} {'hits':>9} {'misses':>8} {'avg ms':>10} {'seconds':>10} {'weights':<24}")
    print("-" * 96)
    for method_name, result in report["methods"].items():
        weights = result.get("weights", {})
        weight_text = ",".join(f"{key}={value:.2f}" for key, value in weights.items())
        print(
            f"{method_name:<18} "
            f"{format_pct(result['r_at_5']):>8} "
            f"{result['hits']:>4}/{result['total']:<4} "
            f"{result['misses']:>8} "
            f"{result['avg_latency_ms']:>10.2f} "
            f"{result['duration_seconds']:>10.2f} "
            f"{weight_text:<24}"
        )
    print()

    print("Per-category Breakdown (all_5_default)")
    print("-" * 48)
    for category, stats in default.get("category_breakdown", {}).items():
        print(f"{category:<18} {format_pct(stats['r_at_5']):>8}  {stats['hits']:>3}/{stats['total']:<3}")
    print()

    print("Per-query-type Breakdown (all_5_default)")
    print("-" * 48)
    for query_type, stats in default.get("query_type_breakdown", {}).items():
        print(f"{query_type:<18} {format_pct(stats['r_at_5']):>8}  {stats['hits']:>3}/{stats['total']:<3}")
    print()

    print("Top-10 Worst-performing Queries (all_5_default misses)")
    print("-" * 96)
    failures = default.get("failures", [])[:10]
    if not failures:
        print("No misses in top-5.")
    for index, failure in enumerate(failures, 1):
        print(f"{index:>2}. fact_id={failure['fact_id']} category={failure['category']} type={failure['query_type']}")
        print(f"    query: {failure['query']}")
        print(f"    top5_ids: {failure['top5_ids']}")
        print(f"    expected: {failure['expected_content']}")
        if failure.get("top5"):
            print(f"    best: {failure['top5'][0]['fact_id']} score={failure['top5'][0]['score']} {failure['top5'][0]['content']}")
        if failure.get("error"):
            print(f"    error: {failure['error']}")
    print()

    print("Test-case Mix")
    print("-" * 48)
    for key, count in sorted(report["test_case_mix"]["categories"].items()):
        print(f"category {key:<14} {count:>3}")
    for key, count in sorted(report["test_case_mix"]["query_types"].items()):
        print(f"query    {key:<14} {count:>3}")


def main() -> int:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Memory store does not exist: {DB_PATH}")

    store = MemoryStore(db_path=DB_PATH)
    conn = store._conn
    conn.row_factory = sqlite3.Row

    facts_total = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
    if facts_total == 0:
        raise RuntimeError("No facts available to benchmark.")

    cases = generate_test_cases(conn, TARGET_CASES)
    if not cases:
        raise RuntimeError("Could not generate benchmark test cases.")

    snapshot = snapshot_mutable_columns(conn)
    method_results: dict[str, Any] = {}
    try:
        for method_name, config in retriever_configs().items():
            method_results[method_name] = run_method(method_name, config, store, cases)
    finally:
        restore_mutable_columns(conn, snapshot)

    category_mix = Counter(case.category for case in cases)
    query_type_mix = Counter(case.query_type for case in cases)

    report = {
        "benchmark": "hermes_holographic_memory_r_at_5",
        "generated_at": now_iso(),
        "database_path": str(DB_PATH),
        "results_path": str(RESULTS_PATH),
        "facts_total": facts_total,
        "test_case_count": len(cases),
        "top_k": TOP_K,
        "random_seed": RANDOM_SEED,
        "state_restored_after_search": True,
        "vector_index_available": bool(getattr(store, "_vec_available", False)),
        "test_case_mix": {
            "categories": dict(sorted(category_mix.items())),
            "query_types": dict(sorted(query_type_mix.items())),
        },
        "test_cases": [
            {
                "case_id": case.case_id,
                "fact_id": case.fact_id,
                "query": case.query,
                "query_type": case.query_type,
                "category": case.category,
                "entities": list(case.entities),
                "expected_content": case.expected_content[:300],
            }
            for case in cases
        ],
        "methods": method_results,
    }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print_report(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
