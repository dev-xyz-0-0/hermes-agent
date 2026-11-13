"""Transactional batch ingestion for the Holographic memory plugin.

Phase 4 of docs/plans/demo-reference-plan/implementation-plan.md. Inspired by
the demo's ``add_facts_batch()``, but it deliberately does NOT bypass
capture/consolidation: every candidate goes through the exact same
`consolidation.plan_candidate()` / `classify_and_apply()`-equivalent decision
logic, the same tombstone policy (via `MemoryStore.remove_fact`'s
``forgotten_facts`` bookkeeping), and the same provenance recording
(`MemoryStore.record_source`) that a single `add_fact`/`classify_and_apply`
call uses. This module adds only two things on top of that: (1) batching
many items into one SQLite transaction via `MemoryStore.transaction()`
instead of one commit per fact, and (2) bounded, reported per-item outcomes.

Deliberately internal/operator-only for this phase (implementation-plan.md
Phase 4, requirement 8): nothing here is wired into `get_tool_schemas()` /
`handle_tool_call()`, so the model cannot call it.

The caller MUST supply an already-resolved namespace — this module never
infers one. Pass ``isolation_blocked=True`` (mirroring
``HolographicMemoryProvider._isolation_blocked``) to make the call refuse to
run at all when the caller's own identity resolution failed, matching the
existing fail-closed pattern for every other read/write path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from . import consolidation
from .capture import FactCandidate

if TYPE_CHECKING:
    from .store import MemoryStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounded batch / item sizes (implementation-plan.md Phase 4, requirement 7).
# Named, documented constants rather than magic numbers so a future tuning
# pass has one place to look. Chosen to comfortably cover realistic operator
# imports (e.g. MEMORY.md/USER.md migration, see __init__.py's
# `import_builtin_memories`) while bounding the cost of a single call: at
# MAX_BATCH_SIZE items, worst case is a few hundred FTS lookups plus one
# transaction, not an unbounded loop.
# ---------------------------------------------------------------------------
MAX_BATCH_SIZE = 500
# Matches capture.py's own `rejection_reason()` "too_long" bound (4000) so a
# batch item is rejected under the identical ceiling a single capture call
# would apply — not a stricter or looser one invented just for batch.
MAX_ITEM_CONTENT_LEN = 4000

# Per-item status enum (implementation-plan.md Phase 4's BatchResult contract).
STATUS_INSERTED = "inserted"
STATUS_DUPLICATE = "duplicate"
STATUS_SUPERSEDED = "superseded"
STATUS_FORGOTTEN = "forgotten"
STATUS_REJECTED = "rejected"
STATUS_NOOP = "noop"
STATUS_FAILED = "failed"

ALL_STATUSES = (
    STATUS_INSERTED, STATUS_DUPLICATE, STATUS_SUPERSEDED,
    STATUS_FORGOTTEN, STATUS_REJECTED, STATUS_NOOP, STATUS_FAILED,
)

# consolidation.ConsolidationResult.action -> our status enum.
_STATUS_FROM_ACTION = {
    "inserted": STATUS_INSERTED,
    "ignored": STATUS_DUPLICATE,
    "superseded": STATUS_SUPERSEDED,
    "forgotten": STATUS_FORGOTTEN,
    "noop": STATUS_NOOP,
}


@dataclass
class SourceContext:
    """Provenance fields recorded for every successful mutation.

    Mirrors exactly what `HolographicMemoryProvider._source()` captures for
    a single interactive add_fact/classify_and_apply call, so a batch item's
    provenance row is indistinguishable in shape from a single-item one.
    """

    via: str = ""
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""
    session_id: str = ""
    author: str = ""
    # Forwarded to add_fact/supersede_fact (not fact_sources) — see
    # MemoryStore.add_fact's source_type/extraction_method/confidence params.
    source_type: str = ""
    extraction_method: str = ""
    confidence: float = 1.0


@dataclass
class BatchItemResult:
    """Outcome for one candidate in the batch."""

    index: int
    status: str
    fact_id: int | None = None
    related_id: int | None = None
    reason: str = ""


@dataclass
class BatchResult:
    """Aggregate outcome of one `ingest_candidates_batch()` call."""

    items: list[BatchItemResult] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in ALL_STATUSES})
    dry_run: bool = False
    atomic: bool = True
    # True once the underlying transaction (or, in non-atomic mode, each
    # item's own commit) has actually been committed. False for a dry run,
    # or for an atomic batch that raised and was rolled back.
    committed: bool = False

    def record(self, item: BatchItemResult) -> None:
        self.items.append(item)
        self.counts[item.status] = self.counts.get(item.status, 0) + 1


def _record_provenance(store: "MemoryStore", fact_id: int, source: SourceContext) -> None:
    store.record_source(
        fact_id,
        via=source.via,
        platform=source.platform,
        chat_id=source.chat_id,
        thread_id=source.thread_id,
        session_id=source.session_id,
        author=source.author,
    )


def _plan_and_apply(
    store: "MemoryStore",
    retriever,
    candidate: FactCandidate,
    *,
    namespace: str,
    category: str,
    source: SourceContext,
    fuzzy_enabled: bool,
    fuzzy_ngram_threshold: float | None,
    dry_run: bool,
    idx: int,
) -> BatchItemResult:
    """Plan (and, unless dry_run, apply) one candidate.

    Reuses `consolidation.plan_candidate()` for the decision and, for real
    writes, the exact same store primitives `consolidation.classify_and_apply()`
    calls (`add_fact` / `supersede_fact` / `remove_fact`) — so insert/
    duplicate/supersede/forget/noop decisions and the tombstone policy inside
    `remove_fact` are identical to the single-item path, not reimplemented.
    """
    content = (candidate.content or "").strip()
    if not content:
        return BatchItemResult(idx, STATUS_NOOP, reason="empty_content")
    if len(content) > MAX_ITEM_CONTENT_LEN:
        return BatchItemResult(
            idx, STATUS_REJECTED,
            reason=f"content_too_long (> {MAX_ITEM_CONTENT_LEN} chars)",
        )

    existing = consolidation._search_existing(retriever, candidate, namespace)
    plan = consolidation.plan_candidate(
        candidate, existing,
        fuzzy_enabled=fuzzy_enabled, fuzzy_ngram_threshold=fuzzy_ngram_threshold,
    )

    if dry_run:
        status = _STATUS_FROM_ACTION.get(plan.action, STATUS_NOOP)
        return BatchItemResult(
            idx, status, fact_id=plan.fact_id, related_id=plan.related_id, reason=plan.reason,
        )

    if plan.action == "superseded":
        new_id = store.supersede_fact(
            plan.related_id, content, category=category, namespace=namespace,
            source_type=source.source_type, extraction_method=source.extraction_method,
            confidence=source.confidence,
        )
        _record_provenance(store, new_id, source)
        return BatchItemResult(idx, STATUS_SUPERSEDED, fact_id=new_id, related_id=plan.related_id, reason=plan.reason)

    if plan.action == "forgotten":
        # tombstone=True (the default) — never silently reintroduces a
        # previously forgotten fact; this IS the existing reintroduction
        # policy (`MemoryStore.remove_fact` -> `forgotten_facts`), reused
        # as-is, not reimplemented for batch.
        store.remove_fact(plan.related_id, namespace=namespace, tombstone=True)
        return BatchItemResult(idx, STATUS_FORGOTTEN, related_id=plan.related_id, reason=plan.reason)

    if plan.action == "inserted":
        fact_id = store.add_fact(
            content, category=category, namespace=namespace,
            source_type=source.source_type, extraction_method=source.extraction_method,
            confidence=source.confidence,
        )
        _record_provenance(store, fact_id, source)
        return BatchItemResult(idx, STATUS_INSERTED, fact_id=fact_id, reason=plan.reason)

    # "ignored" (duplicate) or "noop" (e.g. ambiguous correction/forget target).
    status = STATUS_DUPLICATE if plan.action == "ignored" else STATUS_NOOP
    return BatchItemResult(idx, status, fact_id=plan.fact_id, reason=plan.reason)


def ingest_candidates_batch(
    store: "MemoryStore",
    retriever,
    candidates: Sequence[FactCandidate],
    *,
    namespace: str,
    source: SourceContext,
    category: str = "general",
    isolation_blocked: bool = False,
    atomic: bool = True,
    dry_run: bool = False,
    fuzzy_enabled: bool = True,
    fuzzy_ngram_threshold: float | None = None,
) -> BatchResult:
    """Ingest many `FactCandidate`s through the normal write pipeline.

    Every item runs through the same content validation, scope
    normalization, Phase-3-fuzzy-aware consolidation, and tombstone policy
    that a single `classify_and_apply()` call uses — nothing here
    reimplements or bypasses that logic.

    Args:
        store: an open `MemoryStore`.
        retriever: a `FactRetriever` bound to the same store (used for
            consolidation's duplicate/correction/forget matching).
        candidates: the batch. Rejected with ValueError if larger than
            `MAX_BATCH_SIZE`.
        namespace: the caller's ALREADY-RESOLVED namespace. Never inferred
            here — callers (e.g. a provider wrapper) must resolve it exactly
            as they would for a single add_fact/classify_and_apply call.
        source: provenance fields recorded for every successful mutation.
        category: fact category for inserted/superseded facts.
        isolation_blocked: pass the caller's own fail-closed flag (e.g.
            `HolographicMemoryProvider._isolation_blocked`) through here.
            When True, the whole call is refused before touching the store —
            mirrors the existing fail-closed behavior for every other
            multi-tenant read/write path; it never silently falls back to an
            unscoped namespace.
        atomic: if True (default), every write in the batch happens inside
            one `store.transaction()` — a failure anywhere rolls back every
            mutation attempted so far in THIS call, including earlier
            successful inserts (true atomicity, not partial-then-abort). If
            False, each item commits independently and a failure on one
            item does not affect the others.
        dry_run: if True, performs ZERO writes. Each item's status is the
            action `consolidation.plan_candidate()` would have taken, but
            `add_fact`/`supersede_fact`/`remove_fact` are never called.
        fuzzy_enabled / fuzzy_ngram_threshold: forwarded to
            `consolidation.plan_candidate()` unchanged (Phase 3 config gate).

    Returns:
        A `BatchResult` with one `BatchItemResult` per candidate (in order)
        plus aggregate `counts` across every status in `ALL_STATUSES`.

    Raises:
        PermissionError: `isolation_blocked=True`.
        ValueError: `namespace` is None, or `len(candidates) > MAX_BATCH_SIZE`.
        Exception: whatever an unexpected store/consolidation error raises,
            propagated AFTER rolling back the whole atomic transaction (if
            `atomic=True`) so the caller sees both the error and a
            `BatchResult` reflecting nothing was actually committed.
    """
    if isolation_blocked:
        raise PermissionError(
            "ingest_candidates_batch: refused — caller's namespace isolation "
            "is blocked (missing multi-tenant chat identity); batch ingestion "
            "never falls back to an unscoped or shared namespace."
        )
    if namespace is None:
        raise ValueError("ingest_candidates_batch requires an already-resolved namespace (got None)")
    if len(candidates) > MAX_BATCH_SIZE:
        raise ValueError(f"batch too large: {len(candidates)} candidates > MAX_BATCH_SIZE={MAX_BATCH_SIZE}")

    result = BatchResult(dry_run=dry_run, atomic=atomic)

    def _apply(idx: int, candidate: FactCandidate) -> BatchItemResult:
        return _plan_and_apply(
            store, retriever, candidate,
            namespace=namespace, category=category, source=source,
            fuzzy_enabled=fuzzy_enabled, fuzzy_ngram_threshold=fuzzy_ngram_threshold,
            dry_run=dry_run, idx=idx,
        )

    if dry_run:
        for idx, candidate in enumerate(candidates):
            result.record(_apply(idx, candidate))
        result.committed = False
        return result

    if atomic:
        try:
            # `defer_bank_rebuilds()` nested INSIDE `transaction()` so the
            # one rebuild-per-affected-pair it performs on successful exit
            # is part of the same atomic commit as the facts/entities/
            # sources it was triggered by.
            with store.transaction(), store.defer_bank_rebuilds():
                for idx, candidate in enumerate(candidates):
                    try:
                        item = _apply(idx, candidate)
                    except Exception as exc:
                        logger.warning(
                            "ingest_candidates_batch: item %d raised %s — rolling back whole batch",
                            idx, exc,
                        )
                        result.record(BatchItemResult(idx, STATUS_FAILED, reason=str(exc)))
                        raise
                    result.record(item)
        except Exception:
            # `transaction()` has already rolled back every mutation
            # attempted in this call (including earlier successful items);
            # `defer_bank_rebuilds()` discarded its pending pairs instead of
            # rebuilding them.
            result.committed = False
            return result
        result.committed = True
    else:
        with store.defer_bank_rebuilds():
            for idx, candidate in enumerate(candidates):
                try:
                    item = _apply(idx, candidate)
                except Exception as exc:
                    logger.warning("ingest_candidates_batch: item %d failed (non-atomic): %s", idx, exc)
                    item = BatchItemResult(idx, STATUS_FAILED, reason=str(exc))
                result.record(item)
        result.committed = True

    return result
