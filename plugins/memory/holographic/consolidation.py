"""Scope-aware fact consolidation for PR 2 automatic capture.

Classifies a candidate fact against existing *active* facts and decides
whether to insert, ignore (duplicate/paraphrase), supersede (correction), or
forget (deletion) — using the store's existing FTS5 search, never a new
similarity engine or embedding model.

Deliberately conservative: when scope/intent can't be resolved with
confidence, this inserts a new fact (never silently overwrites) or declines
to delete (never silently forgets the wrong thing). See
``docs/plans/2026-10-15-holographic-pr2-capture-consolidation.md`` for the
tradeoffs behind the overlap thresholds below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .capture import FactCandidate

# Token-overlap thresholds (Jaccard over stopword-stripped content tokens).
# Tuned empirically against the PR 2 spec's worked examples — see the module
# docstring above for where the numbers come from.
_DUPLICATE_OVERLAP = 0.6   # "same fact, different words" -> ignore, don't re-insert
_CORRECTION_OVERLAP = 0.3  # explicit correction -> supersede the best-matching active fact
_FORGET_OVERLAP = 0.25     # forget request -> delete the best-matching active fact

# Stripped in addition to the plugin's generic stopwords: scaffolding verbs
# that recur across every paraphrase of the same fact ("I use X" / "X uses"
# / "I prefer X") and would otherwise inflate overlap without being
# distinctive. This is what lets "My MCP projects use uv" and "I use Python
# uv for MCP projects" match while "Project Alpha uses PostgreSQL" and
# "Project Alpha uses MySQL" do NOT (they differ in the one word that
# matters once scaffolding is removed).
_SCAFFOLD_WORDS = frozenset({
    "i", "my", "me", "mine", "we", "our", "you", "your",
    "use", "uses", "used", "using",
    "prefer", "prefers", "preferred", "preference",
    "is", "are", "am", "the", "a", "an", "for", "to", "of", "on", "in",
    "instead", "now", "then", "currently", "decided", "switch", "switched", "switching",
})

# Require 2+ chars so contraction debris ("i'm" -> "i", "m") doesn't survive
# as a spurious token and dilute/inflate overlap scores.
_RE_TOKEN = re.compile(r"[a-z0-9][a-z0-9_+-]+")


@dataclass
class ConsolidationResult:
    action: str  # "inserted" | "ignored" | "superseded" | "forgotten" | "noop"
    fact_id: int | None = None
    related_id: int | None = None  # the fact superseded, or the fact forgotten
    reason: str = ""


def _content_tokens(text: str) -> set[str]:
    return {t for t in _RE_TOKEN.findall((text or "").lower()) if t not in _SCAFFOLD_WORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _best_match(retriever, query_text: str, compare_text: str, limit: int = 8) -> tuple[dict | None, float]:
    """The active fact whose content best overlaps `compare_text`.

    `query_text` is what's sent to FTS5 to cast a net (may be just a scope
    hint, for recall) — `compare_text` is what overlap is scored against
    (the actual candidate content/forget target, so thresholds mean what
    the module docstring says they mean regardless of how the net was cast).

    Uses FactRetriever.search (not MemoryStore.search_facts directly) because
    it already sanitizes the query for FTS5 — including an OR fallback when
    the AND-of-all-tokens match misses — which a raw MATCH on free-form
    sentence text frequently does.
    """
    try:
        candidates = retriever.search(query_text, min_trust=0.0, limit=limit)
    except Exception:
        candidates = []
    if not candidates and query_text != compare_text:
        try:
            candidates = retriever.search(compare_text, min_trust=0.0, limit=limit)
        except Exception:
            candidates = []
    if not candidates:
        return None, 0.0

    compare_tokens = _content_tokens(compare_text)
    best, best_score = None, 0.0
    for row in candidates:
        score = _overlap(compare_tokens, _content_tokens(row["content"]))
        if score > best_score:
            best, best_score = row, score
    return best, best_score


def classify_and_apply(
    store,
    retriever,
    candidate: FactCandidate,
    *,
    category: str = "general",
    **fact_kwargs,
) -> ConsolidationResult:
    """Apply one candidate fact to the store, picking new/duplicate/correction/forget.

    `fact_kwargs` (source_type, extraction_method, confidence) are forwarded
    to add_fact/supersede_fact for provenance.
    """
    content = (candidate.content or "").strip()
    if not content:
        return ConsolidationResult(action="noop", reason="empty content")

    if candidate.operation == "forget":
        return _apply_forget(store, retriever, candidate)

    query_text = candidate.scope or candidate.old_text or content
    best, best_score = _best_match(retriever, query_text, content)

    if candidate.operation == "correction":
        if best is not None and best_score >= _CORRECTION_OVERLAP and best["content"].strip() != content:
            new_id = store.supersede_fact(best["fact_id"], content, category=category, **fact_kwargs)
            return ConsolidationResult(
                action="superseded", fact_id=new_id, related_id=best["fact_id"],
                reason=f"overlap={best_score:.2f}",
            )
        # No confident correction target — insert standalone rather than guess.
        new_id = store.add_fact(content, category=category, **fact_kwargs)
        return ConsolidationResult(action="inserted", fact_id=new_id, reason="no confident correction target")

    # operation == "statement"
    if best is not None:
        if best["content"].strip() == content:
            return ConsolidationResult(action="ignored", fact_id=best["fact_id"], reason="exact duplicate")
        if best_score >= _DUPLICATE_OVERLAP:
            return ConsolidationResult(action="ignored", fact_id=best["fact_id"], reason=f"paraphrase overlap={best_score:.2f}")

    new_id = store.add_fact(content, category=category, **fact_kwargs)
    return ConsolidationResult(action="inserted", fact_id=new_id, reason="new fact")


def _apply_forget(store, retriever, candidate: FactCandidate) -> ConsolidationResult:
    target = (candidate.content or "").strip()
    if not target:
        return ConsolidationResult(action="noop", reason="empty forget target")

    best, best_score = _best_match(retriever, target, target)
    if best is None:
        return ConsolidationResult(action="noop", reason="no matching fact to forget")
    if best_score < _FORGET_OVERLAP:
        # Ambiguous target — a wrong deletion is worse than a missed one.
        return ConsolidationResult(action="noop", fact_id=best["fact_id"], reason=f"ambiguous target (overlap={best_score:.2f})")

    store.remove_fact(best["fact_id"])
    return ConsolidationResult(action="forgotten", related_id=best["fact_id"], reason=f"overlap={best_score:.2f}")
