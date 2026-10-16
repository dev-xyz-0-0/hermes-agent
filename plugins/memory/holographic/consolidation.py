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


def _scope_text(scope: str) -> str:
    return scope.split(":", 1)[1] if scope.startswith("project:") else scope


def _scope_compatible(scope: str, fact: dict) -> bool:
    """Keep semantic project scope separate from the authorization namespace."""
    if scope in {"", "user"}:
        return True
    if scope == "project":
        return False  # unresolved project scope must never broaden globally
    wanted = _content_tokens(_scope_text(scope))
    present = _content_tokens(str(fact.get("content") or ""))
    return bool(wanted) and wanted.issubset(present)


def _rank_matches(rows: list[dict], compare_text: str, scope: str) -> tuple[dict | None, float, bool]:
    compare_tokens = _content_tokens(compare_text)
    ranked = sorted(
        ((_overlap(compare_tokens, _content_tokens(row.get("content", ""))), row)
         for row in rows if _scope_compatible(scope, row)),
        key=lambda item: item[0], reverse=True,
    )
    if not ranked or ranked[0][0] <= 0:
        return None, 0.0, False
    best_score, best = ranked[0]
    tied = len(ranked) > 1 and abs(ranked[1][0] - best_score) < 0.05
    return best, best_score, tied


def _best_match(
    retriever, query_text: str, compare_text: str, limit: int = 8, namespace: str = "",
) -> tuple[dict | None, float]:
    """The active fact whose content best overlaps `compare_text`.

    `query_text` is what's sent to FTS5 to cast a net (may be just a scope
    hint, for recall) — `compare_text` is what overlap is scored against
    (the actual candidate content/forget target, so thresholds mean what
    the module docstring says they mean regardless of how the net was cast).
    `namespace` scopes the search to one chat/profile's own facts (PR 3) —
    consolidation must never match against, supersede, or forget another
    namespace's fact.

    Uses FactRetriever.search (not MemoryStore.search_facts directly) because
    it already sanitizes the query for FTS5 — including an OR fallback when
    the AND-of-all-tokens match misses — which a raw MATCH on free-form
    sentence text frequently does.
    """
    try:
        candidates = retriever.search(query_text, min_trust=0.0, limit=limit, namespace=namespace)
    except Exception:
        candidates = []
    if not candidates and query_text != compare_text:
        try:
            candidates = retriever.search(compare_text, min_trust=0.0, limit=limit, namespace=namespace)
        except Exception:
            candidates = []
    if not candidates:
        return None, 0.0

    best, best_score, _ = _rank_matches(candidates, compare_text, "")
    return best, best_score


def plan_candidate(candidate: FactCandidate, existing_facts: list[dict]) -> ConsolidationResult:
    """Plan consolidation without opening a store or performing a write."""
    content = (candidate.content or "").strip()
    if not content:
        return ConsolidationResult(action="noop", reason="empty_content")

    scope = candidate.scope or "user"
    if candidate.operation in {"correction", "forget"} and scope == "project":
        return ConsolidationResult(action="noop", reason="unresolved_project_scope")

    if candidate.operation == "correction":
        # Prefer the named previous value. When it is omitted, a named project
        # scope is the minimum safe target; a user/global correction is too broad.
        if candidate.old_text:
            compare = f"{_scope_text(scope)} {candidate.old_text}".strip()
        elif scope.startswith("project:"):
            compare = _scope_text(scope)
        else:
            return ConsolidationResult(action="noop", reason="missing_correction_target")
        best, score, tied = _rank_matches(existing_facts, compare, scope)
        if best is None and not existing_facts:
            return ConsolidationResult(action="inserted", reason="standalone_complete_correction")
        if best is None or score < _CORRECTION_OVERLAP:
            return ConsolidationResult(action="noop", reason="no_confident_correction_target")
        if tied:
            return ConsolidationResult(action="noop", reason="ambiguous_correction_target")
        if best["content"].strip() == content:
            return ConsolidationResult(action="ignored", fact_id=best.get("fact_id"), reason="exact_duplicate")
        return ConsolidationResult(
            action="superseded", related_id=best.get("fact_id"), reason=f"overlap={score:.2f}",
        )

    if candidate.operation == "forget":
        best, score, tied = _rank_matches(existing_facts, content, scope)
        if best is None:
            return ConsolidationResult(action="noop", reason="no_matching_fact_to_forget")
        if tied or score < _FORGET_OVERLAP:
            return ConsolidationResult(
                action="noop", fact_id=best.get("fact_id"), reason="ambiguous_forget_target",
            )
        return ConsolidationResult(action="forgotten", related_id=best.get("fact_id"), reason=f"overlap={score:.2f}")

    best, score, _ = _rank_matches(existing_facts, content, scope)
    if best is not None:
        if best["content"].strip() == content:
            return ConsolidationResult(action="ignored", fact_id=best.get("fact_id"), reason="exact_duplicate")
        if score >= _DUPLICATE_OVERLAP:
            return ConsolidationResult(action="ignored", fact_id=best.get("fact_id"), reason=f"paraphrase_overlap={score:.2f}")
    return ConsolidationResult(action="inserted", reason="new_fact")


def _search_existing(retriever, candidate: FactCandidate, namespace: str) -> list[dict]:
    scope_text = _scope_text(candidate.scope or "")
    query = candidate.old_text or scope_text or candidate.content
    try:
        rows = retriever.search(query, min_trust=0.0, limit=8, namespace=namespace)
    except Exception:
        rows = []
    if not rows and query != candidate.content:
        try:
            rows = retriever.search(candidate.content, min_trust=0.0, limit=8, namespace=namespace)
        except Exception:
            rows = []
    return rows


def classify_and_apply(
    store,
    retriever,
    candidate: FactCandidate,
    *,
    category: str = "general",
    namespace: str = "",
    **fact_kwargs,
) -> ConsolidationResult:
    """Apply one candidate fact to the store, picking new/duplicate/correction/forget.

    `namespace` (PR 3) scopes every match/write to one chat/profile's own
    facts — consolidation never reads, supersedes, or deletes across
    namespaces. `fact_kwargs` (source_type, extraction_method, confidence)
    are forwarded to add_fact/supersede_fact for provenance.
    """
    content = (candidate.content or "").strip()
    if not content:
        return ConsolidationResult(action="noop", reason="empty content")

    plan = plan_candidate(candidate, _search_existing(retriever, candidate, namespace))
    if plan.action == "superseded":
        new_id = store.supersede_fact(plan.related_id, content, category=category, namespace=namespace, **fact_kwargs)
        plan.fact_id = new_id
    elif plan.action == "forgotten":
        store.remove_fact(plan.related_id, namespace=namespace, tombstone=True)
    elif plan.action == "inserted":
        plan.fact_id = store.add_fact(content, category=category, namespace=namespace, **fact_kwargs)
    return plan


def _apply_forget(store, retriever, candidate: FactCandidate, *, namespace: str = "") -> ConsolidationResult:
    # Backwards-compatible private entry point; all policy lives in plan_candidate.
    return classify_and_apply(store, retriever, candidate, namespace=namespace)
