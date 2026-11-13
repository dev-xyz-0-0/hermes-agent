"""Fixed query recall benchmark for holographic memory.

Run to measure search recall against known facts.
Usage:
    python3 scripts/holographic_benchmark.py recall
"""

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

QUERIES = [  # (query, expected_content_substring)
    ("PyCharm Python IDE", "PyCharm"),
    ("BOE NB product development", "BOE"),
    ("memory system architecture", "memory"),
    ("LLM extraction facts", "extract"),
    ("entity relationships", "relationship"),
    ("HRR vector algebra", "HRR"),
    ("FTS5 search query", "FTS5"),
    ("trust score feedback", "trust"),
    ("privacy filter API keys", "privacy"),
    ("MCP server configuration", "MCP"),
]

class RecallBenchmark:
    """Fixed query set to measure recall@5 over time."""

    def __init__(self, store, retriever):
        self.store = store
        self.retriever = retriever

    def run(self, queries=None, k=5, verbose=True):
        """Run benchmark and return (recall, details)."""
        queries = queries or QUERIES
        hits = 0
        total = len(queries)
        details = []

        for query, expected in queries:
            results = self.retriever.search(query, limit=k)
            found = any(expected.lower() in r.get("content", "").lower() for r in results)
            if found:
                hits += 1
            details.append({
                "query": query,
                "expected": expected,
                "found": found,
                "result_count": len(results),
            })
            if verbose:
                status = "✓" if found else "✗"
                print(f"  {status} query='{query}' → expected='{expected}' found={found}")

        recall = hits / total if total > 0 else 0.0
        return {"recall": recall, "hits": hits, "total": total, "details": details}

    def report(self, result):
        """Pretty-print benchmark result."""
        print(f"\n{'='*50}")
        print(f"Recall@{result['total']}: {result['recall']:.1%} ({result['hits']}/{result['total']})")
        print(f"{'='*50}")
        return result


if __name__ == "__main__":
    from holographic_memory import FactRetriever, MemoryStore
    store = MemoryStore()
    retriever = FactRetriever(store)
    bench = RecallBenchmark(store, retriever)
    result = bench.run()
    bench.report(result)
