"""Namespace-scoped entity relationship service for the Holographic plugin.

Phase 5 of docs/plans/demo-reference-plan/implementation-plan.md. Production
already had an `entity_relationships`-shaped need described by the demo, but
no complete namespace-safe service — this module adds one, on top of the raw
storage primitives added to `MemoryStore` in store.py (`upsert_relationship`,
`remove_relationship`, `list_relationships_for_entity`,
`relationship_neighbors`, `invalidate_relationships_for_fact`,
`mark_relationships_stale_for_fact`). Same module split as retrieval.py
(ranking logic) sitting above store.py (raw SQL) for facts.

Critical correctness requirement this module exists to uphold: entity ROWS
remain shared/global (`MemoryStore._resolve_entity` keys only on name/alias,
never namespace — see store.py), but relationship OWNERSHIP is always
namespace-scoped. Two chats whose facts happen to resolve the same entity
NAME to the same global `entity_id` must never see each other's edges just
because the ID matches. Every function here takes an explicit `namespace`
and passes it straight through to the store's namespace-filtered queries —
nothing here ever infers scope from an entity row, and nothing falls back to
an unscoped or "guessed" namespace when the caller doesn't supply one.

Not wired into `get_tool_schemas()`/`handle_tool_call()` in this phase
(implementation-plan.md Phase 5: "Do not expose relationship mutation
directly to the model in the first release") and not invoked from the
synchronous capture/response path (no automatic relationship extraction is
implemented in this phase — see the module docstring note on `9.6`/checklist
section 8, neither of which lists an extraction-pattern requirement for
Phase 5; it is left for the Phase 6 `backfill-relationships` command).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .store import MemoryStore

# Must match HolographicMemoryProvider._DEFAULT_SHARED_NAMESPACE
# (plugins/memory/holographic/__init__.py). Duplicated as a constant here
# (rather than imported) to avoid a relationships.py -> __init__.py import
# edge — __init__.py already imports several store-layer modules and this
# keeps the dependency direction one-way. The value is a well-established,
# non-configurable internal constant, not something expected to drift.
SHARED_NAMESPACE = "__shared__"

# Bounded breadth-first traversal defaults (implementation-plan.md Phase 5 /
# configuration table: relationship_max_depth=2, relationship_max_nodes=50).
# max_edges and deadline_seconds are this module's own additions — the plan
# requires "maximum depth, node, edge, and time limits" but only names
# defaults for the first two.
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_NODES = 50
DEFAULT_MAX_EDGES = 200
DEFAULT_DEADLINE_SECONDS = 2.0

_VALID_DIRECTIONS = frozenset({"outgoing", "incoming", "both"})


@dataclass
class RelationshipEdge:
    """One relationship row, as returned by list/traversal operations."""

    relationship_id: int
    source_entity_id: int
    target_entity_id: int
    relationship_type: str
    namespace: str
    strength: float
    confidence: float
    # True when this edge was read from the shared namespace via the
    # existing shared-memory allowlist mechanism (see `traverse`'s docstring
    # for the category->relationship_type mapping decision), not the
    # caller's own namespace.
    shared: bool = False


@dataclass
class TraversalLimits:
    """The four required bounds for `traverse` (implementation-plan.md
    Phase 5: "Traversal must have maximum depth, node, edge, and time
    limits.")."""

    max_depth: int = DEFAULT_MAX_DEPTH
    max_nodes: int = DEFAULT_MAX_NODES
    max_edges: int = DEFAULT_MAX_EDGES
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS


@dataclass
class TraversalResult:
    start_entity_id: int
    nodes: set = field(default_factory=set)
    edges: list = field(default_factory=list)
    truncated: bool = False
    # Which limit stopped traversal early, or None if it exhausted the graph
    # naturally: "max_depth" | "max_nodes" | "max_edges" | "deadline".
    truncated_reason: "str | None" = None


def _row_to_edge(row: dict, *, shared: bool) -> RelationshipEdge:
    return RelationshipEdge(
        relationship_id=row["relationship_id"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        relationship_type=row["relationship_type"],
        namespace=row["namespace"],
        strength=row["strength"],
        confidence=row["confidence"],
        shared=shared,
    )


def upsert_relationship(
    store: "MemoryStore",
    source_entity_id: int,
    target_entity_id: int,
    relationship_type: str,
    *,
    namespace: str = "",
    strength: float = 0.5,
    confidence: float = 1.0,
    extraction_method: str = "",
    source_fact_id: "int | None" = None,
    target_fact_id: "int | None" = None,
) -> int:
    """Add or update one namespace-scoped relationship edge. See
    `MemoryStore.upsert_relationship` for the exact upsert semantics."""
    return store.upsert_relationship(
        source_entity_id, target_entity_id, relationship_type,
        namespace=namespace, strength=strength, confidence=confidence,
        extraction_method=extraction_method,
        source_fact_id=source_fact_id, target_fact_id=target_fact_id,
    )


def remove_relationship(
    store: "MemoryStore",
    source_entity_id: int,
    target_entity_id: int,
    relationship_type: str,
    *,
    namespace: str = "",
) -> bool:
    """Remove one relationship edge. Returns True if a row existed."""
    return store.remove_relationship(source_entity_id, target_entity_id, relationship_type, namespace=namespace)


def invalidate_for_forgotten_fact(store: "MemoryStore", fact_id: int, *, namespace: "str | None" = None) -> int:
    """Hard-delete relationships derived only from a fact that was forgotten.

    Call this BEFORE `MemoryStore.remove_fact(fact_id, tombstone=True)`, not
    after: `source_fact_id`/`target_fact_id` are foreign keys into
    `facts(fact_id)`, so deleting the fact first raises a foreign-key
    violation while a relationship row still references it. Not invoked
    automatically by `remove_fact` itself (this module is not imported by
    store.py, to keep the storage layer free of relationship policy).
    Returns the number of edges removed.
    """
    return store.invalidate_relationships_for_fact(fact_id, namespace=namespace)


def mark_stale_for_superseded_fact(store: "MemoryStore", fact_id: int, *, namespace: "str | None" = None) -> int:
    """Mark relationships derived from a fact that was just superseded as
    'stale' so they stop counting as active evidence in list/traversal
    results (which default to `status="active"`). Call this alongside
    `MemoryStore.supersede_fact(old_fact_id, ...)`, passing the OLD fact_id
    (the one being replaced) — not invoked automatically, for the same
    reason as `invalidate_for_forgotten_fact` above. Returns the number of
    edges marked stale.
    """
    return store.mark_relationships_stale_for_fact(fact_id, namespace=namespace)


def list_relationships(
    store: "MemoryStore",
    entity_id: int,
    *,
    namespace: str = "",
    direction: str = "both",
    relationship_type: "str | None" = None,
    include_shared: bool = False,
    shared_relationship_types: "frozenset[str] | None" = None,
    limit: int = 200,
) -> list[RelationshipEdge]:
    """List relationships touching ``entity_id``, scoped to ``namespace``.

    ``include_shared``/``shared_relationship_types`` reuse the EXACT same
    two-flag shared-memory mechanism `HolographicMemoryProvider` already
    uses for facts (`_shared_memory_enabled` + `_shared_memory_categories`,
    see __init__.py's `prefetch()`/`_handle_share_action`) rather than
    inventing a parallel one. The only adaptation is what gets allowlisted:
    facts are allowlisted by `category`; relationships have no category
    field, so the caller's `shared_memory_categories` config is reused
    against `relationship_type` instead — a deliberate, documented mapping,
    not a new policy surface. Pass the caller's own
    `_shared_memory_enabled` as `include_shared` and `_shared_memory_categories`
    as `shared_relationship_types` to get identical gating semantics.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {sorted(_VALID_DIRECTIONS)}, got {direction!r}")

    rows = store.list_relationships_for_entity(
        entity_id, namespace=namespace, direction=direction,
        relationship_type=relationship_type, status="active", limit=limit,
    )
    edges = [_row_to_edge(r, shared=False) for r in rows]

    if include_shared and shared_relationship_types:
        shared_rows = store.list_relationships_for_entity(
            entity_id, namespace=SHARED_NAMESPACE, direction=direction,
            relationship_type=relationship_type, status="active", limit=limit,
        )
        edges.extend(
            _row_to_edge(r, shared=True)
            for r in shared_rows
            if r["relationship_type"] in shared_relationship_types
        )
    return edges


def traverse(
    store: "MemoryStore",
    start_entity_id: int,
    *,
    namespace: str = "",
    limits: "TraversalLimits | None" = None,
    include_shared: bool = False,
    shared_relationship_types: "frozenset[str] | None" = None,
) -> TraversalResult:
    """Bounded breadth-first traversal from ``start_entity_id``.

    Enforces all four required limits (implementation-plan.md Phase 5):
    ``max_depth`` (BFS levels explored), ``max_nodes`` (distinct entities
    visited, including the start node), ``max_edges`` (relationship rows
    returned), and ``deadline_seconds`` (wall-clock). Terminates safely on
    cycles via the ``visited`` set: an edge closing a cycle (pointing back
    at an already-visited node) is still recorded, but that node is never
    re-queued, so the frontier can only shrink toward empty, never loop.

    ``include_shared``/``shared_relationship_types`` is the same reused
    shared-memory mechanism documented on `list_relationships` above,
    applied per BFS level.
    """
    limits = limits or TraversalLimits()
    deadline = time.monotonic() + max(0.0, limits.deadline_seconds)

    visited = {start_entity_id}
    edges: list[RelationshipEdge] = []
    edge_ids: set = set()
    frontier: list[int] = [start_entity_id]
    truncated = False
    reason: "str | None" = None

    for _depth in range(max(0, limits.max_depth)):
        if not frontier:
            break
        if time.monotonic() >= deadline:
            truncated, reason = True, "deadline"
            break

        rows = [(r, False) for r in store.relationship_neighbors(frontier, namespace=namespace, status="active")]
        if include_shared and shared_relationship_types:
            rows += [
                (r, True)
                for r in store.relationship_neighbors(frontier, namespace=SHARED_NAMESPACE, status="active")
                if r["relationship_type"] in shared_relationship_types
            ]

        next_frontier: list[int] = []
        hit_limit = False
        for row, is_shared in rows:
            if time.monotonic() >= deadline:
                truncated, reason = True, "deadline"
                hit_limit = True
                break
            rid = row["relationship_id"]
            if rid in edge_ids:
                continue
            if len(edges) >= limits.max_edges:
                truncated, reason = True, "max_edges"
                hit_limit = True
                break
            edge_ids.add(rid)
            edges.append(_row_to_edge(row, shared=is_shared))
            for node in (row["source_entity_id"], row["target_entity_id"]):
                if node in visited:
                    continue
                if len(visited) >= limits.max_nodes:
                    truncated, reason = True, "max_nodes"
                    hit_limit = True
                    break
                visited.add(node)
                next_frontier.append(node)
            if hit_limit:
                break
        if hit_limit:
            break
        frontier = next_frontier
    else:
        # Every allotted depth level ran without hitting another limit —
        # if there's still a frontier to explore, max_depth is what stopped
        # us, not a naturally exhausted graph.
        if frontier:
            truncated, reason = True, "max_depth"

    return TraversalResult(
        start_entity_id=start_entity_id, nodes=visited, edges=edges,
        truncated=truncated, truncated_reason=reason,
    )
