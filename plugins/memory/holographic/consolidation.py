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
import unicodedata
from dataclasses import dataclass

from .capture import FactCandidate

# Token-overlap thresholds (Jaccard over stopword-stripped content tokens).
# Tuned empirically against the PR 2 spec's worked examples — see the module
# docstring above for where the numbers come from.
_DUPLICATE_OVERLAP = 0.6   # "same fact, different words" -> ignore, don't re-insert
_CORRECTION_OVERLAP = 0.3  # explicit correction -> supersede the best-matching active fact
_FORGET_OVERLAP = 0.25     # forget request -> delete the best-matching active fact

# --- Phase 3: deterministic character N-gram evidence (additional, NOT a
# replacement for the token-overlap thresholds above) -----------------------
#
# Adapted from the demo's `_jaccard_ngram()` concept (see
# demo-reference/holographic-memory-main/src/holographic_memory/store.py),
# but deliberately NOT ported as-is: the demo applies N-gram similarity over
# raw sentence text and merges on that signal alone inside
# `MemoryStore.add_fact()`. Measured against a labeled corpus (see
# tests/fixtures/holographic/fuzzy_eval_corpus.json and
# tests/plugins/memory/test_holographic_fuzzy_consolidation.py), that shape
# is actively dangerous here: two sentences that share a scaffold/template
# but differ in exactly the one word that matters (e.g. "My favorite color
# is blue." vs "...is red.") score a HIGHER raw N-gram Jaccard than many
# genuine paraphrases, because the shared scaffold dominates the character
# overlap. Plain token-overlap has the same failure mode for the same reason
# (see _DUPLICATE_OVERLAP's docstring examples) — which is exactly why this
# evidence must never be used alone.
#
# This module instead:
#   1. Computes N-grams over the already scaffold-stripped, sorted content
#      tokens (`_content_tokens` via `_fuzzy_tokens`, which additionally
#      folds accents so unicode spelling variants normalize identically) —
#      not the raw sentence — so shared scaffolding can no longer inflate
#      the score on its own.
#   2. Still refuses to call it a duplicate when the two candidates differ
#      by a small number of tokens whose own characters barely overlap
#      (`_is_value_swap`) — i.e. a genuinely different VALUE ("blue" vs
#      "red", "mysql" vs "postgresql", two different phone numbers) rather
#      than a spelling/morphology variant of the same value ("project" vs
#      "projects", "timezone" vs "time zone"). This is what lets corrections
#      still require an explicit, unambiguous target instead of being
#      silently absorbed into fuzzy-duplicate detection.
# Only active, same-namespace, scope-compatible candidates are ever compared
# (see `_fuzzy_duplicate_match`, which reuses `_scope_compatible`).
_FUZZY_NGRAM_SIZE = 3           # character n-gram size
_MAX_FUZZY_INPUT_LEN = 2000     # bound N-gram cost for oversized content
_FUZZY_DUPLICATE_NGRAM_OVERLAP = 0.4  # calibrated against fuzzy_eval_corpus.json (precision=1.0, recall=1.0 there)
_FUZZY_VALUE_SWAP_GUARD = 0.3         # below this, the differing tokens are a different VALUE, not a spelling variant
_FUZZY_VALUE_SWAP_MAX_DIFF = 2        # only consult the guard for small (<=2 token) differences per side

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
    # Phase 3 diagnostics — purely additive, existing callers that only read
    # action/fact_id/related_id/reason are unaffected.
    strategy: str = "token_overlap"  # "exact" | "token_overlap" | "fuzzy_ngram" | "none"
    fuzzy_score: float | None = None  # mean(token_overlap, ngram_overlap) when strategy == "fuzzy_ngram"


def _content_tokens(text: str) -> set[str]:
    return {t for t in _RE_TOKEN.findall((text or "").lower()) if t not in _SCAFFOLD_WORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _strip_accents(text: str) -> str:
    """NFKD-decompose and drop combining marks so accented and unaccented
    spellings of the same word ("café" / "cafe") normalize identically for
    fuzzy evidence. Deliberately separate from `_content_tokens` itself —
    the baseline token-overlap path (duplicate/correction/forget) must stay
    byte-for-byte unchanged; only the new fuzzy-evidence path below needs
    this extra normalization."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _fuzzy_tokens(text: str) -> set[str]:
    """`_content_tokens`, reused as-is, over accent-folded text."""
    return _content_tokens(_strip_accents(text))


def _char_ngrams(text: str, n: int = _FUZZY_NGRAM_SIZE) -> frozenset[str]:
    text = (text or "").strip()[:_MAX_FUZZY_INPUT_LEN]
    if len(text) < n:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[i:i + n] for i in range(len(text) - n + 1))


def _ngram_overlap(a_tokens: set[str], b_tokens: set[str], n: int = _FUZZY_NGRAM_SIZE) -> float:
    """Character n-gram Jaccard over the sorted, joined content-token sets
    (not raw sentence text — see the module-level comment on why)."""
    canon_a = " ".join(sorted(a_tokens))[:_MAX_FUZZY_INPUT_LEN]
    canon_b = " ".join(sorted(b_tokens))[:_MAX_FUZZY_INPUT_LEN]
    set_a, set_b = _char_ngrams(canon_a, n), _char_ngrams(canon_b, n)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _is_value_swap(a_tokens: set[str], b_tokens: set[str]) -> bool:
    """True when the only difference between two candidates is a small
    number of tokens per side whose characters barely overlap with each
    other — a genuinely different VALUE ("blue"/"red", "mysql"/"postgresql",
    two different phone numbers) rather than a spelling/morphology variant
    of the same value ("project"/"projects"). Shared scaffolding inflates
    both the token-overlap and N-gram scores in this shape (see the module
    docstring), so this guard runs regardless of how high those scores are.
    """
    only_a, only_b = a_tokens - b_tokens, b_tokens - a_tokens
    if not only_a or not only_b:
        return False
    if len(only_a) > _FUZZY_VALUE_SWAP_MAX_DIFF or len(only_b) > _FUZZY_VALUE_SWAP_MAX_DIFF:
        return False
    best_cross = max(_ngram_overlap({ta}, {tb}) for ta in only_a for tb in only_b)
    return best_cross < _FUZZY_VALUE_SWAP_GUARD


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


def _fuzzy_duplicate_match(
    compare_text: str, rows: list[dict], scope: str, *, ngram_threshold: float,
) -> tuple[dict | None, float, float]:
    """The active, scope-compatible fact (if any) that clears the fuzzy
    N-gram duplicate bar for `compare_text`. Returns (row, token_overlap,
    ngram_overlap) for the best match, or (None, 0.0, 0.0). Only ever
    compares facts already filtered to the same namespace by the caller
    (`_search_existing`) and to a compatible scope via `_scope_compatible` —
    never cross-namespace, never cross-scope.
    """
    compare_tokens = _fuzzy_tokens(compare_text)
    best: dict | None = None
    best_pair = (0.0, 0.0)
    for row in rows:
        if not _scope_compatible(scope, row):
            continue
        row_tokens = _fuzzy_tokens(row.get("content", ""))
        ngram_score = _ngram_overlap(compare_tokens, row_tokens)
        if ngram_score < ngram_threshold:
            continue
        if _is_value_swap(compare_tokens, row_tokens):
            continue
        token_score = _overlap(compare_tokens, row_tokens)
        pair = (token_score, ngram_score)
        if pair > best_pair:
            best, best_pair = row, pair
    return best, best_pair[0], best_pair[1]


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


def plan_candidate(
    candidate: FactCandidate,
    existing_facts: list[dict],
    *,
    fuzzy_enabled: bool = True,
    fuzzy_ngram_threshold: float | None = None,
) -> ConsolidationResult:
    """Plan consolidation without opening a store or performing a write.

    `fuzzy_enabled` gates the Phase 3 character-N-gram duplicate evidence
    (config: `fuzzy_dedup_enabled`, default True — see __init__.py). It only
    ever affects the plain-statement duplicate check below; corrections and
    forgets keep requiring their existing explicit, unambiguous targets.
    `fuzzy_ngram_threshold` optionally overrides `_FUZZY_DUPLICATE_NGRAM_OVERLAP`
    (config: `fuzzy_dedup_threshold`), clamped to [0, 1].
    """
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
            return ConsolidationResult(
                action="ignored", fact_id=best.get("fact_id"), reason="exact_duplicate", strategy="exact",
            )
        if score >= _DUPLICATE_OVERLAP:
            return ConsolidationResult(
                action="ignored", fact_id=best.get("fact_id"), reason=f"paraphrase_overlap={score:.2f}",
                strategy="token_overlap", fuzzy_score=score,
            )

    if fuzzy_enabled:
        threshold = _FUZZY_DUPLICATE_NGRAM_OVERLAP
        if fuzzy_ngram_threshold is not None:
            threshold = max(0.0, min(1.0, fuzzy_ngram_threshold))
        fuzzy_best, fuzzy_tok, fuzzy_ngram = _fuzzy_duplicate_match(
            content, existing_facts, scope, ngram_threshold=threshold,
        )
        if fuzzy_best is not None:
            return ConsolidationResult(
                action="ignored", fact_id=fuzzy_best.get("fact_id"),
                reason=f"fuzzy_duplicate token={fuzzy_tok:.2f} ngram={fuzzy_ngram:.2f}",
                strategy="fuzzy_ngram", fuzzy_score=round((fuzzy_tok + fuzzy_ngram) / 2, 3),
            )

    return ConsolidationResult(action="inserted", reason="new_fact", strategy="none")


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
    fuzzy_enabled: bool = True,
    fuzzy_ngram_threshold: float | None = None,
    **fact_kwargs,
) -> ConsolidationResult:
    """Apply one candidate fact to the store, picking new/duplicate/correction/forget.

    `namespace` (PR 3) scopes every match/write to one chat/profile's own
    facts — consolidation never reads, supersedes, or deletes across
    namespaces. `fact_kwargs` (source_type, extraction_method, confidence)
    are forwarded to add_fact/supersede_fact for provenance. `fuzzy_enabled`
    / `fuzzy_ngram_threshold` are the Phase 3 config gate — see
    `plan_candidate`'s docstring.
    """
    content = (candidate.content or "").strip()
    if not content:
        return ConsolidationResult(action="noop", reason="empty content")

    plan = plan_candidate(
        candidate, _search_existing(retriever, candidate, namespace),
        fuzzy_enabled=fuzzy_enabled, fuzzy_ngram_threshold=fuzzy_ngram_threshold,
    )
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
