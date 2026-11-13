"""Optional, bounded, content-free retrieval telemetry for the Holographic
memory plugin.

Phase 8 of docs/plans/demo-reference-plan/implementation-plan.md
("Retrieval telemetry and future weight learning"). This module deliberately
does **not** port the demo's ``learn_weights_from_feedback()`` heuristic —
per the plan, that heuristic cannot attribute a helpful/unhelpful signal to a
specific retrieval strategy and can amplify popularity feedback loops. This
phase is **observation-only**: nothing here ever reads an event back to
adjust a scoring weight or threshold at runtime. Offline weight
*recommendations* built from this data are an explicit, separately reviewed
follow-up (implementation-plan.md section 14, "Deferred Follow-ups") — not
implemented here, and no code path in this module or ``retrieval.py`` closes
that loop.

Event schema (table ``retrieval_events`` in store.py's ``_SCHEMA``), and why
each column is safe to keep:

- ``event_id`` (TEXT, random UUID) — the query/event identifier. Generated
  fresh with ``uuid.uuid4()`` at event-creation time, *never* derived from
  the query text itself (a hash of the query could leak content via a
  targeted collision/rainbow-table search against known phrases — the plan
  explicitly calls this out). Doubles as the "pending event" handle that
  ``record_feedback``/``find_latest_pending_event_id`` use to link an
  explicit helpful/unhelpful call back to the right row.
- ``namespace_digest`` (TEXT) — reuses ``diagnostics._namespace_digest``,
  the exact same one-way SHA-1(namespace)[:12] digest already used for
  standard diagnostic output. Never the raw ``platform:chat_id`` string.
- ``fact_id`` (INTEGER, bare) — a fact's SQLite autoincrement primary key.
  Deliberately NOT digested: it is already a plain integer, meaningless
  outside the one local database file it came from, and every other
  fact_store action (search/probe/list/history/...) already returns it
  verbatim to the model. Combined with ``namespace_digest`` it reveals only
  "retrieval event N touched fact #M in some namespace" — no more than the
  diagnostics module's own per-namespace fact counts already disclose. If a
  future change ever makes fact_id feel too revealing in combination with
  some other field, digest it then; today it is not judged to be.
- ``fts_score`` / ``jaccard_score`` / ``hrr_score`` (REAL, nullable) — the
  three component scores ``FactRetriever.search()`` already computes
  in-memory (FTS5 rank normalized to [0,1], Jaccard token overlap, HRR
  cosine-like similarity shifted to [0,1]). Numbers only, never the query or
  fact text that produced them.
- ``temporal_multiplier`` / ``trust_multiplier`` (REAL, nullable) — the
  temporal-decay factor (1.0 when decay is disabled) and the fact's
  trust_score at retrieval time. Numbers only.
- ``final_rank`` (INTEGER) — 1-based position in the *returned* (post-limit)
  result list, not the raw combined score — so telemetry reflects what the
  caller actually saw, matching implementation-plan.md's "final rank (not
  just raw score)" requirement.
- ``helpful`` (INTEGER, nullable: NULL / 0 / 1) — explicit feedback, absent
  until ``record_feedback`` links a fact_feedback('helpful'/'unhelpful')
  call to this event.
- ``algorithm_version`` (TEXT) — mirrors ``diagnostics.ALGORITHM_VERSION`` /
  ``benchmark.ALGORITHM_VERSION`` (both currently "1") for consistency
  across the plugin's telemetry-shaped outputs.
- ``created_at`` (TIMESTAMP) — SQLite ``CURRENT_TIMESTAMP`` at insert time.

Never written here, by design: prompts, fact content/text, entity or person
names, chat IDs, raw namespace strings, or credentials.

Only instruments ``FactRetriever.search()``. ``probe``/``related``/``reason``
score purely by HRR similarity against a single target vector with no
separable FTS/Jaccard component — inventing placeholder zero values for
fields those methods don't actually compute would misrepresent the schema,
so (per the task's own "if unsure, leave it out" guidance) they are left
uninstrumented in this phase; when they fall back to ``search()`` internally
(no numpy, no bank, no vectors), that inner call is already covered.

Every public function here takes the live ``MemoryStore`` and is written to
be called from inside a ``try/except`` at the call site — see
``FactRetriever._record_telemetry`` — so a telemetry failure (DB locked,
disk full, whatever) can never propagate out of or change the return value
of a retrieval call.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from .diagnostics import _namespace_digest

if TYPE_CHECKING:
    from .store import MemoryStore

ALGORITHM_VERSION = "1"

# Default bound for the retrieval_events row-count cap (see
# MemoryStore.prune_retrieval_events). Overridable per-profile via the
# `retrieval_telemetry_retention` config key (implementation-plan.md's
# configuration table); this is only the fallback when that key is unset.
DEFAULT_RETENTION = 1000


def record_retrieval_event(
    store: "MemoryStore",
    *,
    namespace: str,
    results: list[dict],
    components: dict[int, dict[str, Any]],
    algorithm_version: str = ALGORITHM_VERSION,
    retention: int = DEFAULT_RETENTION,
) -> list[str]:
    """Record one telemetry row per entry in ``results`` (already the
    final, ranked, post-limit list a caller will see) and prune the table
    back to ``retention`` rows afterward.

    ``components`` maps ``fact_id -> {"fts_score", "jaccard_score",
    "hrr_score", "temporal_multiplier", "trust_multiplier"}`` — the
    per-fact scoring breakdown the caller computed before final sort/slice.
    A fact_id missing from ``components`` (should not normally happen) gets
    all-None component scores rather than raising.

    Returns the list of generated event_ids, in the same order as
    ``results``. Raises on a genuine storage failure — callers are expected
    to wrap this in try/except so a telemetry failure never breaks
    retrieval (see the module docstring).
    """
    digest = _namespace_digest(namespace or "")
    event_ids: list[str] = []
    for rank, fact in enumerate(results, start=1):
        fact_id = int(fact["fact_id"])
        comp = components.get(fact_id, {})
        event_id = str(uuid.uuid4())
        store.insert_retrieval_event(
            event_id=event_id,
            namespace_digest=digest,
            fact_id=fact_id,
            fts_score=comp.get("fts_score"),
            jaccard_score=comp.get("jaccard_score"),
            hrr_score=comp.get("hrr_score"),
            temporal_multiplier=comp.get("temporal_multiplier"),
            trust_multiplier=comp.get("trust_multiplier"),
            final_rank=rank,
            algorithm_version=algorithm_version,
        )
        event_ids.append(event_id)
    store.prune_retrieval_events(retention)
    return event_ids


def find_latest_pending_event_id(
    store: "MemoryStore", *, fact_id: int, namespace: str,
) -> str | None:
    """The most recent not-yet-answered retrieval event for this fact in
    this namespace, or None. ``namespace`` is digested internally (the
    table only ever stores the digest) so every caller passes the same raw
    namespace string it already has, never a pre-digested one."""
    digest = _namespace_digest(namespace or "")
    return store.find_latest_pending_retrieval_event(int(fact_id), digest)


def record_feedback(store: "MemoryStore", event_id: str, helpful: bool) -> bool:
    """Set the helpful/unhelpful flag on exactly one event, by its opaque
    event_id. Returns True iff a matching row was found and updated."""
    return store.set_retrieval_feedback(event_id, helpful)


def prune_old_events(store: "MemoryStore", retention: int) -> int:
    """Explicit prune entry point (also run automatically after every
    ``record_retrieval_event`` call). Returns the number of rows deleted."""
    return store.prune_retrieval_events(retention)
