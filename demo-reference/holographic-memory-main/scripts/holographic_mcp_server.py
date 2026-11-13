"""
Holographic Memory MCP Server

Wraps the hermes-agent holographic memory store and exposes retrieval
and storage capabilities as standard MCP tools via stdio transport.

CONFIGURATION
=============

claude_desktop_config.json:
    {
        "mcpServers": {
            "holographic-memory": {
                "command": "python3",
                "args": ["$HERMES_HOME/scripts/holographic_mcp_server.py"],
                "env": {
                    "HERMES_HOME": "$HERMES_HOME"
                }
            }
        }
    }

Codex config (codex_config.json or ~/.codex/config.json):
    {
        "tools": [
            {
                "type": "mcp",
                "server": {
                    "command": "python3",
                    "args": ["$HERMES_HOME/scripts/holographic_mcp_server.py"]
                }
            }
        ]
    }

Claude Code settings.json (mcpServers block):
    {
        "mcpServers": {
            "holographic-memory": {
                "command": "python3",
                "args": ["/tmp/hermes-work/holographic_mcp_server.py"]
            }
        }
    }

HERMES_HOME defaults to ~/.hermes if not set in the environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

_JSON_DEFAULT = lambda o: o.decode("utf-8", errors="replace") if isinstance(o, bytes) else str(o)


# ---------------------------------------------------------------------------
# Sys.path setup — must happen before any holographic imports
# ---------------------------------------------------------------------------

# hermes-agent root: needed for hermes_state, hermes_constants (used by MemoryStore)
# Resolve from HERMES_HOME env var, with fallback to hardcoded path
_HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
_HERMES_AGENT_ROOT = Path(_HERMES_HOME) / "hermes-agent"
if not _HERMES_AGENT_ROOT.exists():
    _HERMES_AGENT_ROOT = Path("$HERMES_HOME/hermes-agent")  # fallback
# plugins/memory: needed so `import holographic` resolves the holographic package
_HOLOGRAPHIC_PARENT = _HERMES_AGENT_ROOT / "plugins" / "memory"

for _p in [str(_HERMES_AGENT_ROOT), str(_HOLOGRAPHIC_PARENT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout is reserved for MCP stdio transport)
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("holographic_mcp")

# ---------------------------------------------------------------------------
# FastMCP setup
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "holographic-memory",
    instructions=(
        "Access and manage structured long-term memory with entity resolution, "
        "trust scoring, and HRR-based compositional retrieval. "
        "Use 'search' for keyword lookup, 'probe' to recall everything about an entity, "
        "'reason' for multi-entity intersection queries, 'related' for structural neighbours, "
        "'contradict' for memory hygiene, and 'add'/'list_facts' for CRUD operations. "
        "Use 'consolidate' to deduplicate and prune low-trust facts. "
        "Use 'extract_llm' to trigger LLM-driven fact extraction. "
        "Use 'add_relationship'/'get_relationships'/'get_graph' for entity relationship queries."
    ),
)

# ---------------------------------------------------------------------------
# Lazy initialisation — store/retriever created on first tool call
# ---------------------------------------------------------------------------

_store = None
_retriever = None


def _get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _ensure_init():
    """Return (store, retriever), initialising them on the first call."""
    global _store, _retriever
    if _store is not None:
        return _store, _retriever

    from holographic.store import MemoryStore
    from holographic.retrieval import FactRetriever

    hermes_home = _get_hermes_home()
    db_path = str(hermes_home / "memory_store.db")

    _store = MemoryStore(db_path=db_path)
    _retriever = FactRetriever(store=_store)
    logger.info("Holographic memory initialised: %s", db_path)
    return _store, _retriever


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search(
    query: str,
    category: Optional[str] = None,
    min_trust: float = 0.3,
    limit: int = 10,
) -> str:
    """Search the fact store using hybrid FTS5 + semantic retrieval.

    Combines SQLite full-text search with Jaccard reranking, optional HRR vector
    similarity, and trust-weighted scoring. Results are sorted by composite score
    descending. Retrieval counts are incremented for matched facts.

    Args:
        query: Search query string (e.g., 'editor config', 'deploy process').
        category: Optional category filter — one of 'user_pref', 'project',
                  'tool', or 'general'. Omit to search all categories.
        min_trust: Minimum trust score threshold in [0.0, 1.0] (default 0.3).
                   Higher values return only well-established facts.
        limit: Maximum number of results to return (default 10, max sensible ~50).

    Returns:
        JSON string: {"results": [<fact>, ...], "count": <int>}
        Each fact has: fact_id, content, category, tags, trust_score,
        retrieval_count, helpful_count, created_at, updated_at, score.
    """
    try:
        _, retriever = _ensure_init()
        results = retriever.search(query, category=category, min_trust=min_trust, limit=limit)
        return json.dumps({"results": results, "count": len(results)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("search failed: %s", exc)
        return json.dumps({"error": str(exc), "results": [], "count": 0})


@mcp.tool()
def probe(
    entity: str,
    category: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Retrieve all facts about a specific entity using HRR algebraic recall.

    Uses Holographic Reduced Representation algebra to find facts where the
    entity plays a structural role — this is richer than keyword matching.
    Automatically falls back to FTS5 keyword search if numpy is unavailable.
    Use this before answering questions about a person, project, or concept.

    Args:
        entity: Entity name to probe (e.g., 'Alice', 'backend', 'Python').
        category: Optional category filter — one of 'user_pref', 'project',
                  'tool', or 'general'. Omit to search all categories.
        limit: Maximum number of results to return (default 10).

    Returns:
        JSON string: {"results": [<fact>, ...], "count": <int>}
        Each fact includes a 'score' field indicating entity relevance.
    """
    try:
        _, retriever = _ensure_init()
        results = retriever.probe(entity, category=category, limit=limit)
        return json.dumps({"results": results, "count": len(results)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("probe failed: %s", exc)
        return json.dumps({"error": str(exc), "results": [], "count": 0})


@mcp.tool()
def reason(
    entities: list[str],
    category: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Multi-entity compositional reasoning — vector-space JOIN across entities.

    Algebraically intersects structural connections for multiple entities to find
    facts related to ALL of them simultaneously (AND semantics via min-score).
    Example: reason(['Alice', 'backend']) finds facts where Alice AND backend both
    play structural roles — without relying on keyword co-occurrence.
    Falls back to combined keyword search if numpy is unavailable.

    Args:
        entities: List of entity names to reason about jointly
                  (e.g., ['Alice', 'backend', 'Python']).
        category: Optional category filter — one of 'user_pref', 'project',
                  'tool', or 'general'. Omit to search all categories.
        limit: Maximum number of results to return (default 10).

    Returns:
        JSON string: {"results": [<fact>, ...], "count": <int>}
        Each fact includes a 'score' field; higher = more connected to all entities.
    """
    try:
        _, retriever = _ensure_init()
        results = retriever.reason(entities, category=category, limit=limit)
        return json.dumps({"results": results, "count": len(results)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("reason failed: %s", exc)
        return json.dumps({"error": str(exc), "results": [], "count": 0})


@mcp.tool()
def related(
    entity: str,
    category: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Find facts structurally connected to an entity (broader than probe).

    Unlike probe which finds facts *about* an entity, related finds facts that
    are connected through shared structural context — other entities mentioned
    alongside this one, or content with structural overlap. Useful for exploring
    the neighbourhood of an entity.
    Falls back to keyword search if numpy is unavailable.

    Args:
        entity: Entity name to find related facts for.
        category: Optional category filter — one of 'user_pref', 'project',
                  'tool', or 'general'. Omit to search all categories.
        limit: Maximum number of results to return (default 10).

    Returns:
        JSON string: {"results": [<fact>, ...], "count": <int>}
        Each fact includes a 'score' field indicating structural connection strength.
    """
    try:
        _, retriever = _ensure_init()
        results = retriever.related(entity, category=category, limit=limit)
        return json.dumps({"results": results, "count": len(results)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("related failed: %s", exc)
        return json.dumps({"error": str(exc), "results": [], "count": 0})


@mcp.tool()
def contradict(
    category: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Find potentially contradictory facts for memory hygiene.

    Detects pairs of facts that share entities (same subject) but have low
    content-vector similarity (different claims). High entity overlap + low
    content similarity = potential contradiction. Returns pairs sorted by
    contradiction score descending. Returns empty results if numpy is unavailable
    (requires HRR vector support).

    Args:
        category: Optional category filter — one of 'user_pref', 'project',
                  'tool', or 'general'. Omit to check all categories.
        limit: Maximum number of contradiction pairs to return (default 10).

    Returns:
        JSON string: {"results": [<pair>, ...], "count": <int>}
        Each pair has: fact_a, fact_b, entity_overlap, content_similarity,
        contradiction_score, shared_entities.
    """
    try:
        _, retriever = _ensure_init()
        results = retriever.contradict(category=category, limit=limit)
        return json.dumps({"results": results, "count": len(results)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("contradict failed: %s", exc)
        return json.dumps({"error": str(exc), "results": [], "count": 0})


@mcp.tool()
def add(
    content: str,
    category: str = "general",
    tags: str = "",
) -> str:
    """Add a new fact to the holographic memory store.

    Stores a fact with automatic entity extraction, trust scoring (default 0.5),
    and HRR vector indexing. Deduplicates by content — adding an identical fact
    returns the existing fact_id without modifying the row. A privacy filter
    blocks API keys, passwords, tokens, and other sensitive patterns.

    Args:
        content: The fact content to store. Should be a complete, self-contained
                 statement that will remain meaningful out of context.
        category: Fact category — one of 'user_pref', 'project', 'tool',
                  or 'general' (default).
        tags: Comma-separated tags for additional filtering (e.g., 'python,editor').

    Returns:
        JSON string: {"fact_id": <int>, "status": "added"}
        On duplicate content: {"fact_id": <existing_id>, "status": "added"}
        On error: {"error": "<message>", "fact_id": null, "status": "error"}
    """
    try:
        store, _ = _ensure_init()
        fact_id = store.add_fact(content, category=category, tags=tags)
        return json.dumps({"fact_id": fact_id, "status": "added"})
    except Exception as exc:
        logger.error("add failed: %s", exc)
        return json.dumps({"error": str(exc), "fact_id": None, "status": "error"})


@mcp.tool()
def update_fact(
    fact_id: int,
    content: Optional[str] = None,
    trust_delta: Optional[float] = None,
    tags: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """Update an existing fact in the memory store.

    Supports partial updates — only provided fields are modified.
    Trust delta is applied asymmetrically (+0.05 helpful, -0.10 unhelpful).
    Content changes trigger re-extraction of entities and re-computation of vectors.

    Args:
        fact_id: The ID of the fact to update.
        content: New content text (omit to keep unchanged).
        trust_delta: Trust score adjustment (e.g., 0.05 for helpful, -0.10 for unhelpful).
        tags: New comma-separated tags (omit to keep unchanged).
        category: New category (omit to keep unchanged).

    Returns:
        JSON string: {"updated": true} or {"updated": false} if fact_id not found.
    """
    try:
        store, _ = _ensure_init()
        updated = store.update_fact(
            fact_id,
            content=content,
            trust_delta=trust_delta,
            tags=tags,
            category=category,
        )
        return json.dumps({"updated": updated})
    except Exception as exc:
        logger.error("update_fact failed: %s", exc)
        return json.dumps({"error": str(exc), "updated": False})


@mcp.tool()
def remove_fact(fact_id: int) -> str:
    """Delete a fact from the memory store by its ID.

    Removes the fact, its entity links, and its vector embedding.
    Returns False if the fact_id does not exist.

    Args:
        fact_id: The ID of the fact to delete.

    Returns:
        JSON string: {"removed": true} or {"removed": false} if not found.
    """
    try:
        store, _ = _ensure_init()
        removed = store.remove_fact(fact_id)
        return json.dumps({"removed": removed})
    except Exception as exc:
        logger.error("remove_fact failed: %s", exc)
        return json.dumps({"error": str(exc), "removed": False})


@mcp.tool()
def list_facts(
    category: Optional[str] = None,
    min_trust: float = 0.0,
    limit: int = 50,
) -> str:
    """Browse stored facts ordered by trust score descending.

    Useful for auditing stored facts, reviewing what the memory knows in a
    category, or identifying low-trust facts that may need removal. Does not
    increment retrieval counts.

    Args:
        category: Optional category filter — one of 'user_pref', 'project',
                  'tool', or 'general'. Omit to list all categories.
        min_trust: Minimum trust score threshold in [0.0, 1.0] (default 0.0 = all facts).
        limit: Maximum number of facts to return (default 50).

    Returns:
        JSON string: {"facts": [<fact>, ...], "count": <int>}
        Each fact has: fact_id, content, category, tags, trust_score,
        retrieval_count, helpful_count, created_at, updated_at.
    """
    try:
        store, _ = _ensure_init()
        facts = store.list_facts(category=category, min_trust=min_trust, limit=limit)
        return json.dumps({"facts": facts, "count": len(facts)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("list_facts failed: %s", exc)
        return json.dumps({"error": str(exc), "facts": [], "count": 0})


@mcp.tool()
def health() -> str:
    """Check the health and status of the holographic memory store.

    Returns diagnostic information about the store's operational state,
    including total fact count, vector engine availability, and the database path.
    Can be used to verify connectivity before running other tools.

    Returns:
        JSON string with fields:
          status        — "ok" or "error"
          fact_count    — total number of stored facts
          hrr_available — true if HRR (numpy) is available for algebraic retrieval
          vec_available — true if sqlite-vec is available for dense vector search
          db_path       — absolute path to the SQLite database file
          error         — present only on failure
    """
    try:
        store, _ = _ensure_init()
        fact_count = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        return json.dumps({
            "status": "ok",
            "fact_count": fact_count,
            "hrr_available": store._hrr_available,
            "vec_available": store._vec_available,
            "db_path": str(store.db_path),
        })
    except Exception as exc:
        logger.error("health check failed: %s", exc)
        return json.dumps({"status": "error", "error": str(exc)})


@mcp.tool()
def extract_llm(
    messages_json: str = "[]",
) -> str:
    """Trigger LLM-driven fact extraction from conversation messages (P0).

    Uses an LLM (configurable via plugin config) to analyse conversation messages
    and extract structured facts. Requires ``llm_extraction: true`` in the
    holographic memory plugin configuration.

    Args:
        messages_json: JSON string of conversation messages array
                       (``[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]``).
                       If empty, extraction runs on available session context.

    Returns:
        JSON string with facts_extracted and facts_stored counts.
    """
    try:
        # Load the holographic memory plugin to get the LLM extractor
        from holographic import HolographicMemoryProvider, _load_plugin_config
        config = _load_plugin_config()
        provider = HolographicMemoryProvider(config)
        hermes_home = _get_hermes_home()
        provider.initialize("mcp_extract", hermes_home=str(hermes_home))

        if not provider._llm_extractor:
            return json.dumps({
                "error": "LLM extraction not enabled (set llm_extraction: true in config)",
                "facts_extracted": 0,
                "facts_stored": 0,
            })

        messages = json.loads(messages_json) if messages_json.strip() else []
        facts = provider._llm_extractor.extract_from_conversation(messages)
        stored = 0
        for fact in facts:
            try:
                provider._store_fact(fact)
                stored += 1
            except Exception:
                pass

        provider.shutdown()
        return json.dumps({
            "facts_extracted": len(facts),
            "facts_stored": stored,
        })
    except Exception as exc:
        logger.error("extract_llm failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "facts_extracted": 0,
            "facts_stored": 0,
        })


# ---------------------------------------------------------------------------
# New MCP tools (Task 2: consolidate, Task 3: relationships)
# ---------------------------------------------------------------------------


@mcp.tool()
def consolidate(
    threshold: float = 0.85,
    prune_trust: float = 0.1,
    max_age_days: int = 90,
) -> str:
    """Run memory consolidation: find duplicates and prune low-trust facts.

    Scans the entire fact store for duplicate/near-duplicate facts and removes
    low-trust facts that have aged beyond max_age_days. This is memory hygiene
    — run periodically to keep the store lean.

    Args:
        threshold: Jaccard similarity threshold for duplicate detection (default 0.85).
        prune_trust: Minimum trust score. Facts at or below this and older than
                     max_age_days are removed (default 0.1).
        max_age_days: Age in days for low-trust pruning (default 90).

    Returns:
        JSON string with duplicates_found, facts_pruned, and duplicate_pairs.
    """
    try:
        store, _ = _ensure_init()
        from holographic.consolidation import MemoryConsolidator
        consolidator = MemoryConsolidator(store)
        duplicates = consolidator.find_duplicates(threshold=threshold)
        pruned = consolidator.prune_low_trust(threshold=prune_trust, max_age_days=max_age_days)
        return json.dumps({
            "duplicates_found": len(duplicates),
            "facts_pruned": pruned,
            "duplicate_pairs": [[int(a), int(b)] for a, b in duplicates],
        })
    except Exception as exc:
        logger.error("consolidate failed: %s", exc)
        return json.dumps({"error": str(exc), "duplicates_found": 0, "facts_pruned": 0})


@mcp.tool()
def add_relationship(
    source: str,
    target: str,
    rel_type: str = "related_to",
    strength: float = 1.0,
) -> str:
    """Create or update an explicit relationship between two entities.

    Entities are auto-resolved (created if they don't exist). If the same
    source/target/type combination already exists, its strength is updated.

    Args:
        source: Source entity name.
        target: Target entity name.
        rel_type: Relationship type (e.g. 'reports_to', 'manages', 'works_for').
        strength: Relationship strength [0.0, 2.0] (default 1.0).

    Returns:
        JSON string: {"relationship_id": <int>, "status": "created"}
    """
    try:
        store, _ = _ensure_init()
        rel_id = store.add_relationship(source, target, rel_type, strength)
        return json.dumps({"relationship_id": rel_id, "status": "created"})
    except Exception as exc:
        logger.error("add_relationship failed: %s", exc)
        return json.dumps({"error": str(exc), "relationship_id": None, "status": "error"})


@mcp.tool()
def get_relationships(
    entity: str,
    rel_type: Optional[str] = None,
) -> str:
    """Get all relationships for an entity, optionally filtered by type.

    Args:
        entity: Entity name to query.
        rel_type: Optional relationship type filter (e.g. 'reports_to').

    Returns:
        JSON string with relationships list.
    """
    try:
        store, _ = _ensure_init()
        rels = store.get_relationships(entity, rel_type=rel_type)
        return json.dumps({"relationships": rels, "count": len(rels)}, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("get_relationships failed: %s", exc)
        return json.dumps({"error": str(exc), "relationships": [], "count": 0})


@mcp.tool()
def get_graph(
    entity: str,
    depth: int = 2,
    rel_type: Optional[str] = None,
) -> str:
    """Explore an entity's relationship network via BFS traversal.

    Args:
        entity: Root entity name to start the graph from.
        depth: BFS depth (1 = direct relationships only, default 2).
        rel_type: Optional relationship type filter.

    Returns:
        JSON string with root, nodes, and edges.
    """
    try:
        store, _ = _ensure_init()
        graph = store.get_graph(entity, depth=depth, rel_type=rel_type)
        return json.dumps(graph, default=_JSON_DEFAULT)
    except Exception as exc:
        logger.error("get_graph failed: %s", exc)
        return json.dumps({"error": str(exc), "root": entity, "nodes": [], "edges": []})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
