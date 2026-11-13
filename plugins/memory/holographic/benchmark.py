"""Read-only retrieval benchmark library for the Holographic memory plugin.

This module is an ORIGINAL, independently-written implementation. It is
*inspired* by the retrieval-quality ideas in
``demo-reference/holographic-memory-main/scripts/holographic_benchmark.py``
(Recall@K, per-strategy weight isolation, latency reporting, a
snapshot/restore technique to keep counters untouched) but does not import
that script or any other module under ``demo-reference/``. See
``plugins/memory/holographic/README.md`` for the project's standing policy
that demo-reference code is reference-only and never imported at runtime.

Design goals (see docs/plans/demo-reference-plan/implementation-plan.md
section "Phase 1 — Benchmark and read-only health diagnostics"):

- Importable and testable without a CLI.
- Operates against an explicit database path and an explicit gold-set file
  (never implicit global state).
- Never mutates production retrieval counters, trust scores, or feedback —
  achieved by snapshotting the only columns ``FactRetriever``/``MemoryStore``
  touch on a read path (``retrieval_count``, ``updated_at``) and restoring
  them in a ``finally`` block, so the net effect on the database is zero
  even though the underlying calls are the real, unmodified production
  code path (not a reimplementation of it).
- Deterministic: repeated runs against the same fixture produce identical
  metrics (no randomness, no wall-clock-dependent decay unless the caller
  explicitly asks for a temporal-decay comparison).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import holographic as hrr
from .retrieval import FactRetriever
from .store import MemoryStore

ALGORITHM_VERSION = "1"
SCHEMA_VERSION = "1"

_DEFAULT_TOP_K = 5
_TEMPORAL_DECAY_PROBE_HALF_LIFE = 30  # days, used only for the decay-comparison pass


class BenchmarkError(ValueError):
    """Stable, safe error for malformed gold-set/benchmark input."""


@dataclass(frozen=True)
class GoldCase:
    """One query -> expected-fact mapping in a benchmark gold set."""

    query: str
    expected_fact_id: int
    namespace: str = ""
    category: str | None = None
    min_trust: float = 0.0


@dataclass(frozen=True)
class GoldSet:
    """A small, deterministic, hand-checkable benchmark fixture.

    ``corpus`` entries are inserted in list order into a fresh database by
    :func:`build_fixture_db`, so fact IDs are deterministic (SQLite
    AUTOINCREMENT starting at 1) for anyone using that helper. A gold set
    may also be evaluated against an operator-supplied, already-populated
    database (see :func:`run_benchmark`'s ``db_path`` argument) — in that
    case ``corpus`` is ignored and ``expected_fact_id`` must refer to real
    rows in that database.
    """

    corpus: tuple[dict[str, Any], ...]
    cases: tuple[GoldCase, ...]


def load_gold_set(path: "str | Path") -> GoldSet:
    """Load and validate a gold-set JSON file.

    Raises :class:`BenchmarkError` (never a raw ``KeyError``/``TypeError``)
    on anything malformed, so CLI/test callers get one stable error type.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BenchmarkError(f"gold set not found: {path}")
    except (OSError, UnicodeDecodeError) as exc:
        raise BenchmarkError(f"could not read gold set {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"gold set {path} is not valid JSON: {exc}")

    if not isinstance(raw, dict):
        raise BenchmarkError("gold set must be a JSON object with 'corpus' and 'cases'")

    corpus_raw = raw.get("corpus", [])
    if not isinstance(corpus_raw, list):
        raise BenchmarkError("gold set 'corpus' must be a list")
    corpus: list[dict[str, Any]] = []
    for i, item in enumerate(corpus_raw):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str) or not item["content"].strip():
            raise BenchmarkError(f"gold set corpus[{i}] must be an object with a non-empty 'content' string")
        corpus.append({
            "content": item["content"],
            "category": item.get("category", "general"),
            "namespace": item.get("namespace", ""),
            "tags": item.get("tags", ""),
        })

    cases_raw = raw.get("cases", [])
    if not isinstance(cases_raw, list) or not cases_raw:
        raise BenchmarkError("gold set must contain a non-empty 'cases' list")
    cases: list[GoldCase] = []
    for i, item in enumerate(cases_raw):
        if not isinstance(item, dict):
            raise BenchmarkError(f"gold set cases[{i}] must be an object")
        query = item.get("query")
        expected = item.get("expected_fact_id")
        if not isinstance(query, str) or not query.strip():
            raise BenchmarkError(f"gold set cases[{i}] missing non-empty 'query'")
        if not isinstance(expected, int):
            raise BenchmarkError(f"gold set cases[{i}] missing integer 'expected_fact_id'")
        cases.append(GoldCase(
            query=query,
            expected_fact_id=expected,
            namespace=str(item.get("namespace", "")),
            category=item.get("category"),
            min_trust=float(item.get("min_trust", 0.0)),
        ))

    return GoldSet(corpus=tuple(corpus), cases=tuple(cases))


def build_fixture_db(gold_set: GoldSet, db_path: "str | Path", hrr_dim: int = 64) -> MemoryStore:
    """Materialize ``gold_set.corpus`` into a fresh database at ``db_path``.

    Fact IDs are assigned in corpus order (1, 2, 3, ...) by SQLite
    AUTOINCREMENT, so a gold set's ``expected_fact_id`` values are
    reproducible for anyone who builds the fixture this same way (tests, or
    an operator preparing a private benchmark corpus).
    """
    store = MemoryStore(db_path=db_path, hrr_dim=hrr_dim)
    for item in gold_set.corpus:
        store.add_fact(
            item["content"],
            category=item.get("category", "general"),
            tags=item.get("tags", ""),
            namespace=item.get("namespace", ""),
        )
    return store


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def _snapshot_mutable_columns(conn: sqlite3.Connection) -> dict[int, tuple[int, "str | None"]]:
    rows = conn.execute("SELECT fact_id, retrieval_count, updated_at FROM facts").fetchall()
    return {int(r[0]): (int(r[1] or 0), r[2]) for r in rows}


def _restore_mutable_columns(conn: sqlite3.Connection, snapshot: dict[int, tuple[int, "str | None"]]) -> None:
    conn.executemany(
        "UPDATE facts SET retrieval_count = ?, updated_at = ? WHERE fact_id = ?",
        [(count, updated_at, fact_id) for fact_id, (count, updated_at) in snapshot.items()],
    )
    conn.commit()


def _namespace_by_fact_id(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute("SELECT fact_id, namespace FROM facts").fetchall()
    return {int(r[0]): (r[1] or "") for r in rows}


def count_namespace_leakage(
    result_fact_ids: list[int],
    expected_namespace: str,
    namespace_by_fact_id: dict[int, str],
) -> int:
    """Pure helper: how many of ``result_fact_ids`` belong to a different
    namespace than ``expected_namespace``. Exposed standalone (not only via
    :func:`run_benchmark`) so tests can feed it a deliberately broken
    fixture — e.g. results that were produced by an unscoped/omitted-filter
    query — without needing a real cross-namespace retrieval bug to exist in
    production code first.
    """
    leaks = 0
    for fid in result_fact_ids:
        actual_ns = namespace_by_fact_id.get(fid)
        if actual_ns is not None and actual_ns != expected_namespace:
            leaks += 1
    return leaks


def _run_queries(
    retriever: FactRetriever,
    cases: tuple[GoldCase, ...],
    *,
    top_k: int,
    namespace_by_fact_id: dict[int, str],
) -> dict[str, Any]:
    hits_at_1 = 0
    hits_at_5 = 0
    reciprocal_ranks: list[float] = []
    empty_results = 0
    errors = 0
    leakage = 0
    latencies_ms: list[float] = []
    per_query: list[dict[str, Any]] = []

    for case in cases:
        started = time.perf_counter()
        error: str | None = None
        results: list[dict] = []
        try:
            results = retriever.search(
                case.query,
                category=case.category,
                min_trust=case.min_trust,
                limit=max(top_k, 5),
                namespace=case.namespace,
            )
        except Exception as exc:  # noqa: BLE001 - benchmark must never crash on one bad case
            error = f"{type(exc).__name__}: {exc}"
            errors += 1
        elapsed_ms = (time.perf_counter() - started) * 1000
        latencies_ms.append(elapsed_ms)

        result_ids = [int(r["fact_id"]) for r in results if r.get("fact_id") is not None]
        if not results and error is None:
            empty_results += 1

        leakage += count_namespace_leakage(result_ids, case.namespace, namespace_by_fact_id)

        rank = None
        if case.expected_fact_id in result_ids:
            rank = result_ids.index(case.expected_fact_id) + 1
        if rank == 1:
            hits_at_1 += 1
        if rank is not None and rank <= 5:
            hits_at_5 += 1
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

        per_query.append({
            "query": case.query,
            "expected_fact_id": case.expected_fact_id,
            "namespace": case.namespace,
            "rank": rank,
            "result_count": len(results),
            "latency_ms": round(elapsed_ms, 3),
            "error": error,
        })

    total = len(cases) or 1
    return {
        "query_count": len(cases),
        "recall_at_1": hits_at_1 / total,
        "recall_at_5": hits_at_5 / total,
        "mean_reciprocal_rank": sum(reciprocal_ranks) / total,
        "empty_result_rate": empty_results / total,
        "query_error_rate": errors / total,
        "namespace_leakage_count": leakage,
        "latency_ms": {
            "p50": round(_percentile(latencies_ms, 50), 3),
            "p95": round(_percentile(latencies_ms, 95), 3),
        },
        "per_query": per_query,
    }


_STRATEGY_WEIGHTS = {
    "fts_only": dict(fts_weight=1.0, jaccard_weight=0.0, hrr_weight=0.0),
    "jaccard_only": dict(fts_weight=0.0, jaccard_weight=1.0, hrr_weight=0.0),
    "hrr_only": dict(fts_weight=0.0, jaccard_weight=0.0, hrr_weight=1.0),
    "default": {},
}


def run_benchmark(
    db_path: "str | Path",
    gold_set_path: "str | Path",
    *,
    top_k: int = _DEFAULT_TOP_K,
    hrr_dim: int = 1024,
) -> dict[str, Any]:
    """Evaluate ``gold_set_path`` against the database at ``db_path``.

    Read-only with respect to production state: ``retrieval_count`` and
    ``updated_at`` are snapshotted before running any queries and restored
    in a ``finally`` block, regardless of success or failure. Trust scores
    and feedback counters are never touched by any code path this function
    calls.
    """
    gold_set = load_gold_set(gold_set_path)

    db_path = Path(db_path)
    if not db_path.exists():
        raise BenchmarkError(f"database not found: {db_path}")

    store = MemoryStore(db_path=db_path, hrr_dim=hrr_dim)
    try:
        snapshot = _snapshot_mutable_columns(store._conn)
        namespace_by_fact_id = _namespace_by_fact_id(store._conn)
        try:
            strategy_breakdown: dict[str, Any] = {}
            default_result: dict[str, Any] | None = None
            for strategy_name, weights in _STRATEGY_WEIGHTS.items():
                retriever = FactRetriever(store=store, hrr_dim=hrr_dim, **weights)
                result = _run_queries(
                    retriever, gold_set.cases, top_k=top_k,
                    namespace_by_fact_id=namespace_by_fact_id,
                )
                strategy_breakdown[strategy_name] = result
                if strategy_name == "default":
                    default_result = result

            temporal_decay: dict[str, Any] = {}
            for label, half_life in (("disabled", 0), ("enabled", _TEMPORAL_DECAY_PROBE_HALF_LIFE)):
                retriever = FactRetriever(store=store, hrr_dim=hrr_dim, temporal_decay_half_life=half_life)
                temporal_decay[label] = _run_queries(
                    retriever, gold_set.cases, top_k=top_k,
                    namespace_by_fact_id=namespace_by_fact_id,
                )
        finally:
            _restore_mutable_columns(store._conn, snapshot)
    finally:
        store.close()

    assert default_result is not None
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "schema_version": SCHEMA_VERSION,
        "database": str(db_path),
        "gold_set": str(gold_set_path),
        "numpy_available": bool(hrr._HAS_NUMPY),
        "top_k": top_k,
        "query_count": default_result["query_count"],
        "recall_at_1": default_result["recall_at_1"],
        "recall_at_5": default_result["recall_at_5"],
        "mean_reciprocal_rank": default_result["mean_reciprocal_rank"],
        "empty_result_rate": default_result["empty_result_rate"],
        "query_error_rate": default_result["query_error_rate"],
        "namespace_leakage_count": default_result["namespace_leakage_count"],
        "latency_ms": default_result["latency_ms"],
        "strategy_breakdown": {
            name: {k: v for k, v in result.items() if k != "per_query"}
            for name, result in strategy_breakdown.items()
        },
        "temporal_decay": {
            name: {k: v for k, v in result.items() if k != "per_query"}
            for name, result in temporal_decay.items()
        },
        "per_query": default_result["per_query"],
    }
