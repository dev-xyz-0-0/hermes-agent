# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval.

## Requirements

None — uses SQLite (always available). NumPy optional for HRR algebra.

## Setup

```bash
hermes memory setup    # select "holographic"
```

Or manually:
```bash
hermes config set memory.provider holographic
```

## Config

Config in `config.yaml` under `plugins.hermes-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end (reserved for later work; off by default) |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |
| `mirror_memory_writes` | `true` | Mirror built-in `memory` tool add/replace/remove into holographic facts |
| `prefetch_min_coverage` | `0.3` | Minimum query-term coverage required for a fact to auto-recall (relevance filter) |
| `prefetch_repeat_turns` | `6` | Turns before a recalled fact can be auto-injected again in the same session |
| `auto_recall` | `true` | Automatically inject relevant stored facts before each turn, without a `fact_store` call |
| `recall_top_k` | `5` | Max facts auto-injected per turn (clamped 1-20) |
| `recall_max_tokens` | `700` | Approximate token budget for the auto-injected memory block per turn (clamped 50-4000) |
| `recall_timeout_ms` | `300` | Soft deadline for scanning recall candidates; returns what was found so far instead of blocking (clamped 10-5000) |
| `recall_debug` | `false` | Log sanitized recall diagnostics (candidate/fact counts, timing) — never fact content |
| `auto_capture` | `false` | Automatically extract, consolidate, and store durable facts stated in conversation |
| `extraction_mode` | `rules` | How candidates are extracted. `llm` is accepted but not yet implemented — falls back to `rules` with a one-time warning |
| `max_facts_per_turn` | `5` | Max candidate facts extracted from one user turn (clamped 1-10) |
| `consolidation_enabled` | `true` | Dedupe/supersede/forget via consolidation; if `false`, captured facts are inserted as-is and forget requests are skipped (never guessed at) |
| `capture_timeout_ms` | `500` | Soft deadline for processing one turn's capture candidates (clamped 10-5000) |
| `capture_debug` | `false` | Log sanitized capture diagnostics (operation, action, fact IDs) — never fact content |

### Automatic capture, consolidation, and forgetting

When `auto_capture: true`, `sync_turn()` — the same once-per-turn post-response
hook used by both CLI and gateway runtimes — runs a conservative, local,
rule-based pipeline (`capture.py` + `consolidation.py`, no LLM calls, no new
vector database):

1. **Extract**: deterministic patterns recognize explicit first-person
   statements, decisions, corrections ("switched X to Y", "no longer use
   X"), and forget requests ("forget...", "don't remember..."). Questions,
   hypotheticals ("we're evaluating...", "for example..."), and quoted/
   reported speech are rejected outright — nothing is captured from them.
2. **Firewall**: every candidate goes through the same write firewall used
   by the explicit `fact_store add` tool — credentials, prompt-injection
   phrasing, and encoded blobs are never stored, regardless of how they were
   phrased.
3. **Consolidate**: the candidate is matched against existing *active*
   facts via FTS5 search + stopword-stripped token overlap (no embeddings).
   - Exact or near-duplicate wording → ignored (not re-inserted).
   - An explicit correction with a confident match → the old fact is marked
     `status='superseded'` (not deleted) and the new one becomes active.
   - Same scope, different value, but *no* correction verb → both are kept
     active side by side. This is a deliberate conservative tradeoff: a
     plain restated fact never silently overwrites a prior one — only an
     explicit correction does.
   - A forget request with a confident match → the fact is hard-deleted.
     Forgetting is never represented as a soft/supersede state — a deleted
     fact cannot be recovered by compaction, replay, or a later search.

Superseded (but not forgotten) facts stay in the database for history and
are visible only via `fact_store {"action": "history"}` — normal
`search`/`list`/`probe`/`related`/`reason`/`contradict`/automatic recall
never return them.

Processing one turn is idempotent per `(session_id, user_content)` pair
(bounded in-memory tracking, not a durable message-ID — see the module
docstring in `__init__.py::sync_turn` for the documented limitation), so a
transport replaying the same message (e.g. Telegram redelivery) does not
create duplicate facts.

**Known limitation**: token-overlap matching is approximate. Genuinely
different phrasings that share little vocabulary with an existing fact
(especially a *forget* request that names only the abstract category, e.g.
"forget my programming language preference", rather than the stored value)
may not be matched with high confidence — the pipeline prefers a safe no-op
over a wrong deletion or a wrong merge in that case.

**`extraction_mode: llm`** is accepted in config but not implemented in this
build — a `MemoryProvider` plugin has no handle to the agent's model/routing
layer, and adding one would be a core change outside this PR's scope. Setting
it logs a one-time warning and falls back to `rules`.

**To disable automatic capture** (rollback) without losing recall or
explicit tools, set `auto_capture: false`. Already-captured facts are
unaffected — rolling back only stops new automatic writes.

### Automatic recall

Each turn, the agent calls `prefetch(query)` once (the same call site used by
both the CLI and the gateway/Telegram runtime, via `MemoryManager.prefetch_all()`
in `run_agent.py`). The holographic provider searches stored facts, filters by
relevance (`prefetch_min_coverage`) and a security firewall (credential-like or
instruction-like content is withheld), ranks and deduplicates, and returns at
most `recall_top_k` facts within the `recall_max_tokens` budget. The result is
wrapped in a `<memory-context>` fence by `MemoryManager` and explicitly labeled
as recalled data, not new user input or system instructions — so a malicious
stored fact (e.g. "ignore previous instructions...") cannot gain authority over
the model's behavior.

No `fact_store` tool call is required for ordinary recall. `fact_store` and
`fact_feedback` remain available for explicit search, structured reasoning
(`probe`/`related`/`reason`/`contradict`), adding/editing facts, and manual
diagnostics.

**To disable automatic recall** (rollback) without losing explicit memory
tools, set `auto_recall: false` under `plugins.hermes-memory-store` in
`config.yaml`, or pass `config={"auto_recall": False}` when constructing the
provider directly. The stored facts and `fact_store`/`fact_feedback` tools
are unaffected.

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | 9 actions: add, search, probe, related, reason, contradict, update, remove, list |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |
