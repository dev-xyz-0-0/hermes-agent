# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval. No external services, no network calls, no additional database to run.

Holographic triggered entirely by *phrasing patterns* the automatic-capture regex engine recognizes, or by the model deciding to call the `fact_store`/`fact_feedback` tools. Here's what reliably works, verified against the actual regexes:

**Capture a fact automatically** (only works if `auto_capture: true`) — say something shaped like:
- `I use X` / `I'm using X` / `I prefer X`
- `my <thing> is/uses/runs Y`
- `project X uses Y`
- `I've decided...` / `from now on...` / `we decided/chose...`
- `I always/never/usually <habit>`

**Correct a stored fact** (supersedes the old one, doesn't duplicate it):
- `switched X to Y` / `switched from X to Y`
- `I no longer use X for my Y projects`
- `I don't use X anymore` / `...any more` / `...any longer`

**Forget a fact** (hard-deletes it — must be at the *start* of the sentence):
- `forget X`
- `don't remember X`
- `remove the memory about X`
- `stop remembering X`

**Will never be captured, on purpose** — questions (anything ending in "?"), hypotheticals (`we're evaluating...`, `for example...`, `let's say...`, `imagine...`, `suppose...`), tentative language (`might/may/could switch...`, `considering migrating...`), fully quoted text, and reported speech (`she said...`, `according to...`).

**Explicit tool use (via natural language, model decides when to invoke it)** — no fixed keyword triggers these; you're relying on the model understanding the request and calling `fact_store`:
- "Show my memory diagnostics" → `{"action": "diagnostics"}`
- "Search your memory for X" → `{"action": "search"}`
- "List what you remember about me" → `{"action": "list"}`
- "What did you used to know about X before I corrected it?" → `{"action": "history"}`
- "Back up the memory database" → `{"action": "backup"}`
- "Share that fact to shared memory" → `{"action": "share"}` (only works if `shared_memory_enabled: true` and the fact's category is allowlisted)

**`fact_feedback`** (marking a recalled fact helpful/unhelpful) has **no trigger phrase at all** — nothing in the codebase scans your wording for it; it fires purely at the model's own discretion.

The legacy `auto_extract` mechanism (if enabled instead of/alongside `auto_capture`) uses a narrower, separate set of 5 patterns: `I prefer X (over/instead of Y)`, `I always/never/usually X`, `my favorite/preferred/default <word> is Y`, `we decided/agreed/chose X`, `the project uses/needs/requires X` — but it only runs once at session end, not per-turn.

## Overview

**Features:**
- **Automatic recall** — relevant stored facts are injected before each turn with no `fact_store` call needed (see [Automatic recall](#automatic-recall)).
- **Automatic capture, consolidation, and forgetting** — durable facts stated in conversation are extracted, deduplicated, corrected, and forgotten on request, fully local and deterministic (see [Automatic capture, consolidation, and forgetting](#automatic-capture-consolidation-and-forgetting)).
- **Profile / chat / database isolation** — one gateway process serving many chats keeps each chat's memory separate by default; separate Hermes profiles need separate database files (see [Profile, Chat, and Database Isolation](#profile-chat-and-database-isolation)).
- **Shared memory** — explicit, read-only, allowlisted cross-chat sharing (see [Shared memory](#shared-memory)).
- **Diagnostics** — sanitized operational counters via `fact_store` (see [Diagnostics](#diagnostics)).
- **Backup and restore** — checksum-verified SQLite snapshots with deletion-aware restore (see [Backup and restore](#backup-and-restore)).
- **Namespace-scoped HRR banks** — compositional-retrieval memory banks keyed by `(namespace, category)`, never a cross-tenant global bank (see [Namespace-Scoped HRR Banks](#namespace-scoped-hrr-banks)).
- **Fuzzy duplicate evidence** — deterministic character N-gram evidence that catches paraphrases token-overlap alone misses, without merging conflicting values (see [Fuzzy Duplicate Evidence in Consolidation](#fuzzy-duplicate-evidence-in-consolidation)).
- **Transactional batch ingestion** — an internal/operator-only API for atomic, bounded, provenance-complete multi-fact writes (see [Batch Ingestion](#batch-ingestion)).
- **Namespace-scoped entity relationships** — a bounded graph service with BFS traversal limits, populated by an explicit operator backfill command, not automatic extraction (see [Entity Relationships](#entity-relationships)).
- **Operator maintenance commands** — `health`, `benchmark`, `rebuild-vectors`, `rebuild-banks`, `backfill-relationships`, `verify`, `restore`, and `viewer`, all local, profile-aware, and never exposed to the model or gateway (see [Operator Maintenance Commands](#operator-maintenance-commands)).
- **Optional local diagnostic viewer** — loopback-only, read-only, opaque-by-default (see [Local Diagnostic Viewer](#local-diagnostic-viewer)).
- **Optional retrieval telemetry** — content-free, bounded-retention scoring telemetry for future offline weight evaluation, off by default (see [Retrieval Telemetry](#retrieval-telemetry)).

**Requirements:** None — uses SQLite (always available). NumPy is optional; without it, HRR-only operations (`probe`/`related`/`reason`/`contradict`) fall back to keyword search (or, for `contradict`, an empty result — see [Memory Tools](#memory-tools)).

## Quick Start

### 1. Enable Holographic

```bash
hermes memory setup    # interactive — select "holographic"
```

Or select it directly without the wizard:

```bash
hermes config set memory.provider holographic
```

Both write `memory.provider: holographic` to `config.yaml`. The CLI reads
config fresh on every invocation (no restart needed); a long-running
**gateway** process reads config at startup, so restart it after changing
`memory.provider`.

### 2. Configure automatic recall and capture

Add (or edit) this block in `config.yaml`:

```yaml
plugins:
  hermes-memory-store:
    auto_recall: true
    auto_capture: true
    extraction_mode: rules
    consolidation_enabled: true
    profile_isolation: true
    diagnostics_enabled: true
```

- `auto_recall` (default `true`) — retrieves relevant stored facts automatically, before each turn, no tool call needed.
- `auto_capture` (default **`false`**) — extracts durable facts from what the user says. Off by default so upgrading never silently starts writing facts; this example turns it on intentionally.
- `extraction_mode: rules` (default) — deterministic, local, no model calls. **Don't default to `hybrid`** — it adds auxiliary-model latency/cost and is meant to be opted into deliberately.
- `consolidation_enabled` (default `true`) — dedup/correct (supersede)/forget captured facts instead of inserting as-is.
- `profile_isolation` (default `true`) — scopes facts by gateway chat/platform namespace *within one database*. Does **not** separate Hermes profiles sharing one database file — see [Profile, Chat, and Database Isolation](#profile-chat-and-database-isolation).
- `diagnostics_enabled` (default `true`) — enables `fact_store {"action": "diagnostics"}`.

### 3. Test automatic capture

Send:
```text
I use PostgreSQL for my Solana analytics project.
```
Capture runs once the turn completes, subject to the extraction rules
(first-person statement, not a question/hypothetical), the write firewall,
and consolidation finding no duplicate. Confirm it was actually stored via
[diagnostics](#5-check-diagnostics) (`capture_accepted` counter) or
`fact_store {"action": "list"}` — don't assume it from configuration alone.

### 4. Test automatic recall

In a later turn, ask:
```text
Which database do I use for my Solana analytics project?
```
Hermes calls `prefetch()` once for the turn; if the fact passes the
relevance filter (`prefetch_min_coverage`) and isolation scope, it's
injected into context and the model answers "PostgreSQL" — **no**
`fact_store` call required. This depends on the fact having been captured
in step 3 and on `recall_top_k`/`recall_max_tokens` budget; it isn't
guaranteed by configuration alone.

### 5. Check diagnostics

Ask Hermes in natural language:
```text
Use fact_store diagnostics to show my Holographic memory status.
```
This works only when holographic is the active `memory.provider` (the
`fact_store` tool is added to the model's tool list only when a memory
provider is active). Real, verified response fields:
```json
{
  "diagnostics_enabled": true, "namespace": "", "hrr_available": true,
  "auto_recall": true, "auto_capture": true,
  "database": "/home/you/.hermes/memory_store.db",
  "facts": 3, "active": 3, "superseded": 0,
  "metrics": {"recall_attempts": 2, "recall_success": 1, "capture_accepted": 1}
}
```
`namespace` is the resolved chat/platform scope (`""` for CLI). `metrics`
only lists counters that have incremented at least once — not a fixed set.
`diagnostics_enabled: false` returns just that one field.

### 6. Troubleshooting

| Symptom | Possible cause | What to check |
|---|---|---|
| Fact not captured | Automatic capture disabled | `auto_capture` |
| Fact not captured | Statement rejected by extraction rules | Supported extraction patterns (see [Automatic capture, consolidation, and forgetting](#automatic-capture-consolidation-and-forgetting)) |
| Fact not recalled | Recall disabled | `auto_recall` |
| Fact not recalled | Relevance filter | `prefetch_min_coverage` |
| Wrong memory scope | Database or namespace configuration | Resolved `db_path` and chat namespace (see [Profile, Chat, and Database Isolation](#profile-chat-and-database-isolation)) |
| Diagnostics unavailable | Diagnostics or tool registration disabled | `diagnostics_enabled`, and whether `memory.provider` is actually `holographic` for this session |

## Configuration Reference

All keys live in `config.yaml` under `plugins.hermes-memory-store` (not
`plugins.holographic`).

**Storage and trust**

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `default_trust` | `0.5` | Default trust score assigned to newly added facts |
| `min_trust_threshold` | `0.3` | Minimum trust score for a fact to be included in search/stats/recall filtering (distinct from `default_trust`; a per-call `fact_store` `min_trust` argument overrides this for that call) |
| `hrr_dim` | `1024` | HRR vector dimensions |
| `mirror_memory_writes` | `true` | Mirror built-in `memory` tool add/replace/remove into holographic facts |

**Automatic recall**

| Key | Default | Description |
|-----|---------|-------------|
| `auto_recall` | `true` | Automatically inject relevant stored facts before each turn, without a `fact_store` call |
| `recall_top_k` | `5` | Max facts auto-injected per turn (clamped 1-20) |
| `recall_max_tokens` | `700` | Approximate token budget for the auto-injected memory block per turn (clamped 50-4000) |
| `recall_timeout_ms` | `300` | Soft deadline for scanning recall candidates; returns what was found so far instead of blocking (clamped 10-5000) |
| `recall_debug` | `false` | Log sanitized recall diagnostics (candidate/fact counts, timing) — never fact content |
| `prefetch_min_coverage` | `0.3` | Minimum query-term coverage required for a fact to auto-recall (relevance filter) |
| `prefetch_repeat_turns` | `6` | Turns before a recalled fact can be auto-injected again in the same session |
| `hrr_weight` | `0.3` | Weight given to HRR structural similarity in recall ranking (automatically redistributed to FTS/Jaccard scoring when NumPy is unavailable) |
| `temporal_decay_half_life` | `0` (disabled) | Half-life, in turns, for decaying older facts' recall rank; `0` disables decay |

**Automatic capture**

| Key | Default | Description |
|-----|---------|-------------|
| `auto_capture` | `false` | Automatically extract, consolidate, and store durable facts stated in conversation, via the rule-based `capture.py`/`consolidation.py` pipeline |
| `extraction_mode` | `rules` | `rules` is local/deterministic; `hybrid` uses rules first and an auxiliary-model fallback for eligible user statements. Legacy `llm` values map to `hybrid` |
| `hybrid_capture_timeout_ms` | `5000` | Maximum auxiliary-model time for one hybrid fallback (clamped 100-30000 ms) |
| `max_facts_per_turn` | `5` | Max candidate facts extracted from one user turn (clamped 1-10) |
| `consolidation_enabled` | `true` | Dedupe/supersede/forget via consolidation; if `false`, captured facts are inserted as-is and forget requests are skipped (never guessed at) |
| `capture_timeout_ms` | `500` | Soft deadline for processing one turn's capture candidates (clamped 10-5000) |
| `capture_debug` | `false` | Log sanitized capture diagnostics (operation, action, fact IDs) — never fact content |
| `auto_extract` | `false` | A separate, older, simpler regex-based extractor that runs once at session end (not per-turn, and not the `capture.py` pipeline above). Off by default for the same reason `auto_capture` is: an upgrade should never silently start writing new facts |
| `fuzzy_dedup_enabled` | `true` | Use deterministic character N-gram similarity as additional consolidation evidence for probable paraphrase duplicates (see [Fuzzy Duplicate Evidence in Consolidation](#fuzzy-duplicate-evidence-in-consolidation)); set `false` to return to pure token-overlap matching |
| `fuzzy_dedup_threshold` | *(unset — uses the calibrated `0.4` constant)* | Optional override for the fuzzy-duplicate N-gram overlap bar (clamped `0.0`-`1.0`) |

**Isolation and sharing**

| Key | Default | Description |
|-----|---------|-------------|
| `profile_isolation` | `true` | Scope facts by gateway chat/platform namespace within one database (see [Profile, Chat, and Database Isolation](#profile-chat-and-database-isolation)). Default **true** — unlike most flags above, "off" here means a real cross-chat data leak, not a conservative default |
| `shared_memory_enabled` | `false` | Allow read-only recall from an explicit shared namespace, for allowlisted categories only |
| `shared_memory_categories` | *(empty)* | Comma-separated category allowlist for shared memory (both reading it and the `share` action writing to it) |

**Diagnostics**

| Key | Default | Description |
|-----|---------|-------------|
| `diagnostics_enabled` | `true` | Track operational counters for `fact_store {"action": "diagnostics"}` — counts only, never fact content |
| `diagnostics_include_content` | `false` | Include a category breakdown in diagnostics output (still never raw fact text) |

**Backup and retention**

| Key | Default | Description |
|-----|---------|-------------|
| `backup_enabled` | `false` | Reserved for future automated backup scheduling — the manual `fact_store {"action": "backup"}` action works regardless of this setting |
| `backup_retention` | `14` | Keep the N most recent backups per namespace after each manual backup (`0` = keep all) |

**Retrieval telemetry**

| Key | Default | Description |
|-----|---------|-------------|
| `retrieval_telemetry` | `false` | Record bounded, content-free retrieval-scoring telemetry (see [Retrieval Telemetry](#retrieval-telemetry)); nothing is written while this is `false` |
| `retrieval_telemetry_retention` | `1000` | Maximum `retrieval_events` rows retained (oldest pruned first on every write, clamped `1`-`100000`); only relevant when `retrieval_telemetry` is enabled |

No settings are currently purely experimental beyond `backup_enabled`'s
reserved-for-scheduling note above — every other key already has a live
effect in the current implementation. Note that `scoped_hrr_banks`,
`relationship_recall_enabled`, `relationship_max_depth`, and
`relationship_max_nodes` — named in the demo-reference integration's design
plan — are **not** real config.yaml keys in the shipped code; see
[Configuration Defaults and Rollback Reference](#configuration-defaults-and-rollback-reference)
for why.

## Profile, Chat, and Database Isolation

Holographic isolation works at two independent levels. Confusing them is
the most common source of "wrong memory scope" surprises.

**Database-level profile isolation.** Hermes profiles (`hermes -p <name>`,
a separate `$HERMES_HOME` per invocation) are isolated only because they
typically resolve to a *different database file*. If two profiles are
configured to point at the same `db_path` (directly, or indirectly by
sharing `$HERMES_HOME`), they share the same facts — there is no
additional per-profile gate inside the plugin. **Profiles that must not
share memory must never point to the same SQLite database — including its
WAL and SHM files.** `agent_identity` does **not** participate in
namespace resolution, and `profile_isolation: true` does **not** separate
profiles that share one database. Cross-profile sharing is possible only
by intentionally configuring profiles to point at a common database, or
through a separately implemented sharing mechanism.

**Gateway chat isolation (within one database).** A single long-running
gateway process (Telegram, Discord, Slack, WhatsApp, Signal, Home
Assistant) serving many chats resolves every chat's `HolographicMemoryProvider`
to the *same* database file. Every fact row has a `namespace` column; for
known multi-tenant platforms with a resolvable `chat_id`, the namespace is
`"{platform}:{chat_id}[:{thread_id}]"`, and every read/write (recall,
capture, consolidation, `fact_store`, `on_memory_write` mirroring) is
scoped to it. This is what `profile_isolation: true` actually controls.

**Default namespace.** CLI and other single-tenant-per-process contexts
(cron, subagents) use the original unscoped (`""`) namespace — zero
behavior change for them.

**Fail closed, never fall back.** If a multi-tenant platform session has
no resolvable `chat_id` (a runtime bug or an unusual integration), the
provider sets `_isolation_blocked = True` and every memory operation —
`prefetch`, `sync_turn`, every `fact_store`/`fact_feedback` call — becomes
a safe no-op (or returns an error for tool calls) rather than falling back
to the shared/unscoped bucket. A sanitized warning is logged once.

**To opt back into the old (shared) chat behavior** — not recommended for
multi-tenant deployments — set `profile_isolation: false`. This does not
affect database-level profile isolation either way.

**Cron, subagent, and background-review isolation** work through a
different mechanism entirely: `skip_memory=True` on the `AIAgent`
constructor, which skips `MemoryManager` construction (and therefore this
plugin) before any namespace logic runs. See
[Security and Input Provenance](#security-and-input-provenance). A
provider-level `agent_context` write gate exists in this plugin's code
(keyed on `"cron"`/`"subagent"`/`"flush"`), but nothing in the current
codebase ever passes those values — it is not an actively exercised
runtime mechanism today; `skip_memory=True` is what actually does the
isolating.

## Automatic Recall

Each turn, the agent calls `prefetch(query)` once (the same call site used
by both the CLI and the gateway/Telegram runtime, via
`MemoryManager.prefetch_all()` in `run_agent.py`). The holographic provider
searches stored facts, filters by relevance (`prefetch_min_coverage`) and a
security firewall (credential-like or instruction-like content is
withheld), ranks and deduplicates, and returns at most `recall_top_k` facts
within the `recall_max_tokens` budget. The result is wrapped in a
`<memory-context>` fence by `MemoryManager` and explicitly labeled as
recalled data, not new user input or system instructions — see
[Recall firewall](#recall-firewall).

No `fact_store` tool call is required for ordinary recall. `fact_store` and
`fact_feedback` remain available for explicit search, structured reasoning
(`probe`/`related`/`reason`/`contradict`), adding/editing facts, and manual
diagnostics.

**To disable automatic recall** (rollback) without losing explicit memory
tools, set `auto_recall: false` under `plugins.hermes-memory-store` in
`config.yaml`, or pass `config={"auto_recall": False}` when constructing the
provider directly. The stored facts and `fact_store`/`fact_feedback` tools
are unaffected.

## Automatic Capture, Consolidation, and Forgetting

When `auto_capture: true`, `sync_turn()` — the same once-per-turn
post-response hook used by both CLI and gateway runtimes — runs a
conservative, local, rule-based pipeline (`capture.py` + `consolidation.py`,
no LLM calls, no new vector database):

1. **Extract**: deterministic patterns recognize explicit first-person
   statements, decisions, corrections ("switched X to Y", "no longer use
   X", "I don't use X anymore"), and forget requests ("forget...", "don't
   remember..."). Questions, hypotheticals ("we're evaluating...", "for
   example..."), and quoted/reported speech are rejected outright — nothing
   is captured from them. With `extraction_mode: hybrid`, a non-question
   user message that produces no rule candidate is sent to the configured
   auxiliary text model on a background worker. Its JSON output is
   untrusted candidate data and still passes every check below. Assistant
   responses are never extraction input.
2. **Firewall**: every candidate goes through the same write firewall used
   by the explicit `fact_store add` tool — credentials, recovery/seed
   phrases, prompt-injection phrasing, and encoded blobs are never stored,
   regardless of how they were phrased.
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

**`extraction_mode: hybrid`** broadens phrasing coverage at auxiliary-model
cost and may finish shortly after the response is displayed. If no
auxiliary provider is available, output is malformed, or the deadline
expires, capture fails closed and diagnostics record the reason. The old
`llm` value is a deprecated alias for `hybrid`. It is not the default, and
this README does not recommend making it the default.

**Separately, `auto_extract: true`** enables an older, simpler regex-based
extractor (`_auto_extract_facts()`) that runs once at session end rather
than per-turn — five fixed patterns ("I prefer...", "I always/never/
usually...", "my favorite X is Y", "we decided...", "the project uses...").
It is independent of the `capture.py`/`consolidation.py` pipeline described
above and does not run consolidation. Off by default for the same reason:
an upgrade should never silently start writing new facts.

### Importing built-in memory

Existing `MEMORY.md` and `USER.md` entries are not silently copied during
an upgrade. Preview an idempotent, profile-scoped migration with:

```bash
hermes memory import --from builtin --provider holographic
```

The preview lists proposed, duplicate, and rejected entries without
writing. Apply exactly that migration explicitly with `--apply`. Imported
entries pass the same write firewall, retain their user/general category,
and record `builtin_import` provenance.

**To disable automatic capture** (rollback) without losing recall or
explicit tools, set `auto_capture: false`. Already-captured facts are
unaffected — rolling back only stops new automatic writes.

## Shared Memory

Disabled by default. When `shared_memory_enabled: true` and
`shared_memory_categories` lists one or more categories, every session with
sharing enabled can additionally **read** (never automatically write) facts
from a fixed shared namespace, filtered to those categories. Shared facts
are tagged `[shared:<trust>]` in the injected recall context for provenance.

The *only* way a fact reaches the shared namespace is the explicit
`fact_store {"action": "share", "fact_id": <id>}` tool action, which copies
one of the caller's own active facts into shared memory — and only if its
category is allowlisted and it passes the write firewall (credentials are
rejected just like any other write). There is no implicit write-through:
an ordinary `add`/capture never lands in shared memory. Disabling
`shared_memory_enabled` on a session immediately stops it from reading
shared facts (already-shared facts aren't deleted, but nothing reads them
without the flag).

## Diagnostics

`fact_store {"action": "diagnostics"}` returns a sanitized operational
snapshot. Verified real top-level fields: `diagnostics_enabled`,
`namespace`, `profile_isolation`, `hrr_available`, `auto_recall`,
`auto_capture`, `consolidation_enabled`, `shared_memory_enabled`,
`backup_enabled`, `database`, `facts`, `active`, `superseded`, `metrics`
(a dict of counters that only appear once incremented — e.g.
`recall_attempts`, `recall_success`, `capture_attempts`,
`capture_accepted`, `capture_rejected`, `capture_duplicate`,
`consolidation_superseded`, `consolidation_forgotten`, `feedback_attempts`,
`feedback_helpful`, `feedback_unhelpful`, `sqlite_errors`), and `by_category`
(only present when
`diagnostics_include_content: true`). It never includes raw fact content,
and works even against a down/erroring store (so the error counters
themselves stay reachable). Set `diagnostics_enabled: false` to get a
minimal `{"diagnostics_enabled": false}` stub instead (counters still
aren't collected in that case). See the
[Quick Start example](#5-check-diagnostics) for a full sample response.

**Scope and persistence of `metrics`:** counters live in a plain in-memory
dict on the provider instance (`self._metrics`), never written to SQLite.
They reset to empty on every `initialize()` call — a new session, a profile
switch, or a gateway restart — so they are a snapshot of activity since the
last initialize, not a durable running total. Because one provider instance
is also re-initialized (and its namespace recomputed) each time it's handed
a different chat/session, `metrics` is not a per-namespace running total
either — it reflects whichever namespace was last active, not an aggregate
across all namespaces the process has ever served. `facts`/`active`/
`superseded`, by contrast, come straight from SQLite and are both durable
and namespace-scoped.

### Context-overhead attribution

The shared CLI and gateway commands `/context`, `/context all`, and
`/context diagnose` report the estimated prompt overhead of Holographic
memory that has already been materialized. Static attribution uses the
frozen memory boundary in the cached system prompt; dynamic attribution uses
the fenced recall block already produced for the current turn. Running these
commands does not call `prefetch()`, search the store, initialize a provider,
or change retrieval counters.

The displayed state is `available` when a session-owned measurement exists,
`unavailable` when Hermes cannot safely recover a current boundary, and
`disabled` when Holographic is not active. An available zero-token dynamic
measurement means the turn produced no recall block. After a gateway restart,
static or dynamic attribution can be unavailable because this diagnostic
metadata is intentionally in-memory and no new persistence schema was
introduced.

Attribution is an approximate token-overhead measurement, not a stored-fact
count. Diagnostic output never includes recalled facts, prompt contents,
credentials, resolved namespaces, or database paths.

### `hermes memory health` and `hermes memory benchmark` (operator CLI)

These are separate from the model-callable `fact_store {"action":
"diagnostics"}` tool action above: they are operator-only `hermes memory`
subcommands, read-only, never exposed to the model and never wired into
gateway slash commands or Telegram/Slack command menus.

```bash
hermes memory health [--json] [--backup-path PATH]
hermes -p NAME memory health [--json]

hermes memory benchmark --gold-set PATH [--json]
hermes -p NAME memory benchmark --gold-set PATH [--json]
```

Both commands resolve the target database through the normal
`HERMES_HOME`/profile mechanism (the global `-p`/`--profile` flag, handled
in `hermes_cli/main.py` before any module import) — there is no separate
`--profile` flag on the subcommand itself, and neither command accepts a
raw chat namespace or chat identity argument. `--json` output never
includes decorative/banner text; the entire stdout payload is a single
JSON object.

**`hermes memory health`** runs `plugins/memory/holographic/diagnostics.py`
against the resolved database, opened with SQLite's `mode=ro` URI flag so
the connection itself cannot write, independent of any bug in the checks.
It checks: `PRAGMA integrity_check` and `foreign_key_check`; expected
tables/columns/indexes/FTS triggers; FTS5 index/facts parity (compared
against the FTS5 shadow `_docsize` table, not a plain row count, which is
unreliable for an external-content FTS5 table); orphaned `fact_entities`/
`fact_sources` rows; invalid or cyclic `superseded_by` chains; malformed or
wrong-dimension HRR vector blobs; `memory_banks` dimension/count validity;
and facts with an invalid `status` or an out-of-range `trust_score`.

**`--backup-path PATH` changes the diagnosed target, not just an extra
check**: when given, every check above (and every count) runs against
*that backup file*, not the live database — `backup.py`'s
`verify_backup()` runs first to checksum/integrity-check the file, and only
if that passes does the health suite open the backup itself (`mode=ro`) and
diagnose it. The live database is never consulted for `issues`/`counts` in
this mode; `JSON` field `target` reads `"backup"`, `db_path` is the backup
file, and `live_db_path` names the live database purely for context (it was
not diagnosed). Omit `--backup-path` to diagnose the live database
(`target: "live"`, `live_db_path: null`). JSON fields: `ok`,
`algorithm_version`, `schema_version`, `exit_code`, `target`, `db_path`,
`live_db_path`, `issues` (each with a stable `code`/`severity`/`message`/
`count` — e.g. `FTS_PARITY_MISMATCH`, `ORPHAN_FACT_ENTITY`,
`INVALID_SUPERSEDED_BY`, `SUPERSESSION_CYCLE`, `MALFORMED_HRR_VECTOR`,
`BANK_COUNT_MISMATCH`, `INVALID_FACT_STATUS`, `INVALID_TRUST_SCORE`,
`BACKUP_INVALID`), `counts` (facts/active/superseded/entities/tombstones/
memory_banks — always describing whichever database was diagnosed),
`namespaces` (per-namespace counts only — each namespace is reported as
`""` for unscoped/CLI facts or a short opaque SHA-1 digest, never the raw
`platform:chat_id` string), and `backup` (the checksum/integrity verify
result; independent of `issues`/`counts`). Exit codes are stable: `0` =
clean, `1` = warnings only, `2` = errors found (including an invalid
backup, which stops diagnostics before opening the file further), `3` =
could not run (e.g. database not found).

**`hermes memory benchmark`** runs
`plugins/memory/holographic/benchmark.py` against the resolved database and
an operator-supplied gold-set JSON file (see
`tests/fixtures/holographic/gold_set.json` for the format: a `corpus` of
facts and a list of `cases`, each a `query` → `expected_fact_id` mapping).
It is read-only with respect to production state: `retrieval_count` and
`updated_at` are snapshotted before running any queries and restored
afterward in a `finally` block, so repeated runs never inflate retrieval
counters, and trust scores/feedback counters are never touched by any code
path it calls. JSON fields: `algorithm_version`, `schema_version`,
`database`, `gold_set`, `numpy_available`, `top_k`, `query_count`,
`recall_at_1`, `recall_at_5`, `mean_reciprocal_rank`, `empty_result_rate`,
`query_error_rate`, `namespace_leakage_count` (always expected to be `0`;
a nonzero value means a result's fact belonged to a different namespace
than the query was scoped to), `latency_ms` (`p50`/`p95`), `strategy_breakdown`
(`fts_only`/`jaccard_only`/`hrr_only`/`default` — isolated retrieval-weight
configurations, to see each strategy's individual contribution),
`temporal_decay` (`disabled`/`enabled`), and `per_query` detail. Exit codes:
`0` on success, `2` on a malformed gold set, `3` if the database does not
exist.

These commands are an original implementation inspired by (not imported
from) the retrieval-quality and health-check ideas in
`demo-reference/holographic-memory-main/scripts/holographic_benchmark.py`
and `.../holographic_memory_health.py`. **No code under `demo-reference/`
is imported by `plugins/memory/holographic/` or any other runtime module —
it exists purely as read-only reference material for this integration.**

## Backup and Restore

`fact_store {"action": "backup"}` snapshots the live database using
SQLite's online backup API (safe against a concurrently open WAL
connection — no file copy, no pausing writers) to
`$HERMES_HOME/backups/holographic/<namespace>/memory-<timestamp>.db`, with a
SHA-256 checksum sidecar and `0600` permissions. `backup_retention` prunes
older backups automatically after each manual backup.

**Restore is deliberately NOT a `fact_store` tool action** — it's exposed
only as `HolographicMemoryProvider.restore_from_backup(path)` (library/
provider level) and `hermes memory restore` (the P1 operator CLI below),
never from chat. A prompt-injected or confused model triggering a restore
could silently reactivate facts the user explicitly forgot; that decision
belongs to a human.

### `hermes memory restore` (operator CLI)

```bash
hermes memory restore --from PATH [--dry-run | --apply] [--allow-backup-warnings] [--json]
hermes -p NAME memory restore --from PATH ...
```

Full-DB replace (not a merge), dry-run by default — mirrors the
dry-run/apply/lock/backup shape of the other maintenance commands, but with
a richer result shape since there's a destination DB to diff against and a
set of hard gates to report, not just planned counts.

**Dry-run** (default, zero writes): verifies the backup's checksum/
integrity, diagnoses the backup DB and the live DB *independently* (P0
semantics — never mixes their counts/issues into each other), reports a
`facts`/`active`/`memory_banks` diff between them, counts tombstones on the
live DB (`tombstones_live_count` — these block the matching fact from being
reactivated by the restore), and computes the exact same apply-gate logic
`--apply` will use (`gates.apply_allowed`, `gates.block_reasons`). Safe to
run against a target that doesn't exist yet (a fresh profile) — `live_health`
is `null` and the diff is skipped, nothing else changes.

**Apply gates (hard-block, no live mutation on any of these):**
1. `--from` missing/unreadable → exit `3` (cannot run)
2. Backup fails checksum/integrity verification → exit `2`
3. Backup health has **error**-severity issues → exit `2`
4. Backup health has **warning**-severity issues and `--allow-backup-warnings`
   was not passed → exit `1` (pass the flag to proceed anyway; errors always
   block regardless of the flag)

**Apply** (only once gates pass): acquires the maintenance lock, re-verifies
the backup inside it (TOCTOU-safe), re-reads live tombstones, then delegates
the actual file-level work to the pre-existing `backup.restore_backup()`
primitive described below — safety-copies the current live DB first (kept
indefinitely, never auto-deleted), sweeps any tombstoned fact out of the
staged backup before it ever touches the live file, and atomically replaces
the target. Reports `safety_copy_path`, `reactivated_blocked`, and
`post_health` (a full `hermes memory health`-equivalent diagnosis of the
now-restored live DB). No auto-rollback on a dirty post-health result in v1
— recover manually from `safety_copy_path` if needed (see the Operator
procedure below).

Exit codes match the rest of the maintenance commands: `0` ok, `1`
warnings-only (e.g. dry-run with `apply_allowed: false` purely due to
backup-health warnings), `2` errors, `3` cannot run, `4` maintenance lock
held. Stop the gateway/any other process holding this profile's DB open
before `--apply` — a live WAL writer during the file swap is unsafe (v1 does
not detect this automatically).

Only handles native Holographic `memory-*.db` + `.json` sidecar backups
(the ones `fact_store {"action": "backup"}` / maintenance commands create).
It does **not** restore skills-pack's encrypted cross-host snapshots — that
remains a separate tool (`hermes_backup.py restore`, migration-to-fresh-
destination only).

**Operator procedure:**

1. **Create a backup** via chat or the memory tool: `fact_store {"action": "backup"}`. Note the path it reports under `$HERMES_HOME/backups/holographic/<namespace>/`.
2. **Check the backup's own health**: `hermes memory health --backup-path PATH --json` (P0) — diagnoses the backup file itself, not the live DB.
3. **Dry-run the restore**: `hermes memory restore --from PATH --json` — review `diff`, `tombstones_live_count`, and `gates.apply_allowed`/`block_reasons`.
4. **Stop processes accessing the database** — close the live store (end the Hermes/gateway process using it) before restoring; restore does a file-level swap, which an open WAL connection could otherwise corrupt.
5. **Apply**: `hermes memory restore --from PATH --apply --json`. Note the `safety_copy_path` it reports.
6. **Verify restored data** — run `hermes memory health --json` (included as `post_health` in the apply result already) and spot-check expected facts via `fact_store {"action": "list"}` / `{"action": "search"}`.
7. **Forgotten-fact tombstones are handled automatically** — restore reads every `forgotten_facts` tombstone (recorded whenever a fact is explicitly removed, across all namespaces) *before* touching the live file, and re-deletes any matching row the backup would otherwise reintroduce, so a backup taken before an explicit forget can't bring that fact back.
8. **If restoration fails** — the backup's checksum and `PRAGMA integrity_check` are verified twice (on the backup itself, then again on the staged copy) *before* touching anything live, and a timestamped safety copy of the pre-restore database is kept indefinitely. If a failure is reported, the live database should be untouched; recover by inspecting that safety copy and the reported issue, rather than retrying blindly. Manual rollback:
   ```bash
   cp -a /path/to/memory_store.db.pre-restore-TIMESTAMP.bak /path/to/memory_store.db
   rm -f /path/to/memory_store.db-wal /path/to/memory_store.db-shm
   hermes memory health --json
   ```

## Namespace-Scoped HRR Banks

A "bank" is a bundled HRR vector representing every active fact in one
`(namespace, category)` pair — used to make `probe`/`related`/`reason`
queries cheap without comparing against every fact individually. Earlier
versions kept one bank per category (`cat:<category>`), aggregated across
*all* namespaces; because that aggregation would leak structure across
tenants, scoped retrieval simply never used the bank optimization and fell
back to scanning per-fact.

**Current behavior.** `bank_name(namespace, category)` (in `store.py`)
derives a deterministic, opaque key:

- `namespace == ""` (the unscoped/CLI identity) keeps the original
  `cat:<category>` key verbatim, so every bank written before this change
  remains valid with no migration — this is the one case that keeps a
  human-readable (but still non-identity-revealing) key.
- Every other namespace is folded through a short SHA-1 digest — never the
  raw `platform:chat_id` string — producing `cat:ns:<digest16>:<category>`.
  This is the same style of digest `diagnostics.py`'s `_namespace_digest`
  already uses for sanitized diagnostic output.

`rebuild_bank(category, namespace="")` rebuilds exactly one `(namespace,
category)` bank from that namespace's own *active* facts only — superseded
facts are excluded by the `status != 'superseded'` filter, and forgotten
facts can never contribute because `remove_fact` hard-deletes the row.
Every fact mutation path (`add_fact`, `supersede_fact`, `remove_fact`,
category/namespace changes on `update`) calls `rebuild_bank` for just the
pair(s) it affected — never a bulk rebuild of every bank in the database.
Inside `store.transaction()` combined with `defer_bank_rebuilds()` (used by
batch ingestion, see [Batch Ingestion](#batch-ingestion) below), rebuilds
for the same pair collapse into a single rebuild performed once at commit,
not once per fact.

**There is no "legacy global bank" fallback path for scoped queries.** A
scoped `probe`/`related`/`reason` call only ever looks up the bank keyed by
its own `bank_name(namespace, category)` — it has no code path that reads a
`cat:<category>` bank on behalf of a non-empty namespace. If a scoped bank
doesn't exist yet (not rebuilt, or zero facts in that pair), the HRR query
simply returns no bank-optimized hits for that pair rather than ever
substituting another namespace's aggregate.

**Legacy-bank cleanup.** Old cross-namespace `cat:<category>` rows are
never deleted automatically — they are harmless (nothing scoped reads them,
and unscoped CLI retrieval still legitimately uses the `cat:<category>`
key) and remain available for forensic inspection. An operator can force a
full, current rebuild of every `(namespace, category)` pair with `hermes
memory rebuild-banks --apply` (see [Operator Maintenance
Commands](#operator-maintenance-commands)); this does not special-case or
delete legacy rows, it simply rebuilds the pairs that currently have active
facts.

**No configuration flag.** Unlike fuzzy dedup or telemetry below, scoped
banking is not gated by a `scoped_hrr_banks`-style config key — there is no
such key in `get_config_schema()`. Namespacing the bank key is structural,
always-on behavior for every database opened by this version of the
plugin; "rolling back" scoped banks therefore does not mean flipping a
switch, it means **not relying on the bank optimization at all** — which is
exactly the automatic, already-tested fallback: whenever a bank entry is
absent (or NumPy is unavailable — see [NumPy
Degradation](#numpy-degradation) below), `probe`/`related`/`reason`/`search`
degrade to their existing per-fact scoped scoring. That per-fact path was
never bank-based to begin with, so this fallback never crosses a namespace
boundary either.

## Fuzzy Duplicate Evidence in Consolidation

Automatic capture's consolidation step (see [Automatic Capture,
Consolidation, and Forgetting](#automatic-capture-consolidation-and-forgetting))
originally matched candidates against existing active facts using only
stopword-stripped token-overlap (Jaccard over whole words). That catches
reworded sentences that still share most of their vocabulary, but misses
paraphrases that restate a fact in different words built on the same
sentence scaffold.

**What was added**, in `consolidation.py`, as *additional* evidence —
never a replacement for the token-overlap thresholds, and never invoked
from `MemoryStore.add_fact()` itself:

1. Tokens are extracted the same way as token-overlap matching
   (`_content_tokens`), but additionally accent-folded
   (`_strip_accents`) so unicode spelling variants normalize identically,
   and a fixed set of scaffolding words is stripped (`_SCAFFOLD_WORDS`:
   pronouns, "use/uses/used/using", "prefer/preferred", "switch/switched",
   articles, and a few more) — words that recur across almost any
   paraphrase of the same fact and would otherwise inflate similarity
   without being distinctive.
2. Character 3-grams (`_FUZZY_NGRAM_SIZE = 3`) are computed over the
   sorted, joined, scaffold-stripped token sets (not the raw sentence —
   this matters, see below) and compared by Jaccard overlap
   (`_ngram_overlap`). A candidate clears the fuzzy-duplicate bar at
   `_FUZZY_DUPLICATE_NGRAM_OVERLAP = 0.4`, calibrated against the checked-in
   evaluation corpus (`tests/fixtures/holographic/fuzzy_eval_corpus.json`).
3. **The value-swap guard (`_is_value_swap`)** — the critical safeguard that
   makes this safe to enable. Two sentences that share a scaffold/template
   but differ in exactly the one word that matters ("My favorite color is
   blue." vs "...is red.") score a *higher* raw N-gram Jaccard than many
   genuine paraphrases, because the shared scaffold dominates character
   overlap — scaffold-stripped token overlap has the same failure mode for
   the same reason. Before accepting a fuzzy match, the guard checks
   whether the two candidates differ by only a small number of tokens per
   side (`_FUZZY_VALUE_SWAP_MAX_DIFF = 2`) whose characters barely overlap
   with each other (`_FUZZY_VALUE_SWAP_GUARD = 0.3`) — that shape means a
   genuinely different *value* ("blue"/"red", "mysql"/"postgresql", two
   different phone numbers), not a spelling/morphology variant of the same
   value ("project"/"projects"). When the guard fires, the candidate is
   **not** treated as a fuzzy duplicate, regardless of how high its raw
   N-gram score is.

Only active, same-namespace, scope-compatible facts are ever compared
(`_fuzzy_duplicate_match` reuses the same `_scope_compatible` check
token-overlap matching already used) — fuzzy evidence never crosses a
namespace boundary and never widens the existing user/project scope rules.
Security rejection (the write firewall) always runs before any similarity
comparison, fuzzy or token-based, on every write path.

**Diagnostics, not a new result shape.** `ConsolidationResult` gained two
purely additive fields — `strategy` (`"exact"` / `"token_overlap"` /
`"fuzzy_ngram"` / `"none"`) and `fuzzy_score` (the mean of token-overlap and
N-gram overlap, set only when `strategy == "fuzzy_ngram"`) — existing
callers that only read `action`/`fact_id`/`related_id`/`reason` are
unaffected.

**Documented limitation — deep-synonym paraphrases are not caught by
design.** This is character-level N-gram similarity over tokens, not a
semantic/embedding model. A paraphrase built from *different words with no
shared characters* for the key concept (e.g. "I use Postgres" vs "my
relational database is Postgres" — if the differing words themselves share
little string overlap) will not clear the N-gram bar even though a human
would recognize it as the same fact. This is a deliberate, documented
tradeoff (see the module's own comment on why plain N-gram-over-raw-text,
as the demo does it, was rejected): a conservative miss (insert as a
second, independent fact) is always preferred over a wrong merge. Explicit
corrections ("switched X to Y") still require the existing, unambiguous
correction-overlap threshold (`_CORRECTION_OVERLAP`), which fuzzy evidence
does not loosen.

**Configuration.** `fuzzy_dedup_enabled` (default `true`) gates whether
fuzzy evidence is consulted at all; set it to `false` to return to the
original token-overlap-only behavior (the pre-Phase-3 rollback path named
in implementation-plan.md's rollout/rollback plan). `fuzzy_dedup_threshold`
(default unset, meaning "use the calibrated `_FUZZY_DUPLICATE_NGRAM_OVERLAP`
constant") optionally overrides the N-gram overlap bar; when set, it is
clamped to `[0.0, 1.0]`. Neither setting affects the token-overlap
thresholds (`_DUPLICATE_OVERLAP`/`_CORRECTION_OVERLAP`/`_FORGET_OVERLAP`),
which have no configuration override.

Fuzzy dedup is pure string/token processing — it has no dependency on
NumPy and behaves identically whether or not NumPy is installed.

## Batch Ingestion

`plugins/memory/holographic/batch.py`'s `ingest_candidates_batch()` is an
**internal/operator-only API** — it is not wired into `get_tool_schemas()`/
`handle_tool_call()`, so the model cannot call it, and no gateway slash
command exposes it either. It exists to let a future bulk-import or
backfill path (e.g. a larger `MEMORY.md`/`USER.md` migration) write many
facts without one SQLite commit per fact, while still running every
candidate through the *same* write pipeline a single `fact_store add` /
automatic-capture call uses — content validation, scope normalization,
Phase-3-fuzzy-aware consolidation, and the same tombstone policy
(`MemoryStore.remove_fact`'s `forgotten_facts` bookkeeping). Nothing about
batch ingestion bypasses capture, consolidation, or security checks.

**Bounded limits.** `MAX_BATCH_SIZE = 500` candidates per call (a
`ValueError` beyond that). `MAX_ITEM_CONTENT_LEN = 4000` characters per
candidate's content — the same ceiling `capture.py`'s own
`rejection_reason()` already applies to a single capture candidate, so a
batch item is rejected under an identical bound, not a stricter or looser
one invented for batch.

**Atomicity.** With `atomic=True` (the default), every item in the call
runs inside one `store.transaction()`: a failure anywhere — including an
unexpected exception partway through — rolls back *every* mutation
attempted in that call, including earlier items that had already
succeeded. `defer_bank_rebuilds()` is nested inside the same transaction, so
the one rebuild-per-affected-`(namespace, category)`-pair it performs on
successful exit is part of the same atomic commit as the facts/entities/
sources that triggered it; on rollback, the pending rebuild set is simply
discarded rather than applied. With `atomic=False`, each item commits (and
can fail) independently — a failure on one item never affects the others.

**Dry-run.** `dry_run=True` performs **zero writes**: each item's reported
status is exactly the action `consolidation.plan_candidate()` would have
taken, but `add_fact`/`supersede_fact`/`remove_fact` are never actually
called.

**Per-item status enum** (`BatchItemResult.status`, one of
`batch.ALL_STATUSES`): `inserted`, `duplicate` (consolidation judged it an
exact/fuzzy match of an existing active fact), `superseded` (an explicit
correction replaced an existing fact), `forgotten` (an explicit forget
request deleted — and tombstoned — a match), `rejected` (failed validation,
e.g. oversized content), `noop` (e.g. an ambiguous correction/forget target
— deliberately not guessed at), and `failed` (an unexpected exception for
that item; only meaningful in non-atomic mode, since an atomic batch's
failure aborts the whole call). `BatchResult.counts` aggregates how many
items landed in each status.

**Namespace is always caller-supplied, never inferred.** The caller must
pass an already-resolved `namespace`; passing `isolation_blocked=True`
(mirroring `HolographicMemoryProvider._isolation_blocked`) makes the whole
call refuse up front with a `PermissionError`, before touching the store —
the same fail-closed behavior every other multi-tenant read/write path
already has. Batch ingestion never falls back to an unscoped or shared
namespace when identity resolution fails.

**Provenance** is recorded for every successful mutation via the same
`record_source()`/`SourceContext` shape a single interactive `add_fact` call
uses, so a batch item's provenance row is indistinguishable in shape from
one written by a normal `fact_store add`.

**Built-in memory import was deliberately NOT migrated onto this API in
this phase** — `import_builtin_memories` (used by `hermes memory import
--from builtin --provider holographic`) still uses its original,
independently-tested code path. Proving byte-for-byte behavioral
equivalence between the two was judged riskier than the available task
budget allowed; the batch API was added standalone instead, and the
existing import path's tests still pass unmodified. A future migration of
that import path onto `ingest_candidates_batch()` remains open work, not a
regression.

## Entity Relationships

`entities`/`fact_entities` (which entity names a fact mentions) remain
shared, global-ID resources — `MemoryStore._resolve_entity` keys only on
name/alias, never on namespace, exactly as before this integration.
**Relationship *ownership*, however, is always namespace-scoped.** The new
`entity_relationships` table carries a `namespace` column from creation,
with a composite unique index on `(namespace, source_entity_id,
target_entity_id, relationship_type)` — created defensively inside
`MemoryStore._migrate_entity_relationships()` rather than inline in the
schema script, specifically so a differently-shaped pre-existing
`entity_relationships` table (hypothetical today, since production had none
before this phase) is backfilled with the missing columns/index before the
index is created, rather than the `CREATE INDEX` failing against a
not-yet-migrated table. Each row also stores `strength` (clamped
confidence-like weight, default 0.5), `confidence` (default 1.0),
`extraction_method`, optional `source_fact_id`/`target_fact_id` foreign
keys into `facts`, a `status` (`'active'` or `'stale'`, mirroring
`facts.status`), and timestamps.

**Why namespace scoping matters here specifically:** two chats whose facts
happen to resolve the same entity *name* ("PostgreSQL") to the same global
`entity_id` must never see each other's relationship edges just because the
ID matches. Every function in `relationships.py` takes an explicit
`namespace` and passes it straight through to namespace-filtered store
queries — nothing infers scope from the entity row, and nothing falls back
to an unscoped or guessed namespace when a caller omits one.

**Service API** (`relationships.py`): `upsert_relationship` /
`remove_relationship` (idempotent — upserting the same edge twice updates
strength/confidence rather than duplicating a row), `list_relationships`
(direct listing for one entity, with an optional read from the shared
namespace gated by the *same* `shared_memory_enabled` /
`shared_memory_categories` flags facts already use — relationships have no
`category` field, so `shared_memory_categories` is reused against
`relationship_type` instead, a deliberate, documented mapping rather than a
new policy surface), `traverse` (bounded breadth-first graph walk),
`invalidate_for_forgotten_fact` (hard-deletes relationships derived only
from a fact that was just forgotten — call this *before*
`remove_fact(tombstone=True)`, since `source_fact_id`/`target_fact_id` are
foreign keys), and `mark_stale_for_superseded_fact` (marks relationships
derived from a just-superseded fact `status='stale'` so they stop counting
as active evidence in list/traversal results, without hard-deleting them).

**Traversal bounds** (`TraversalLimits`, all four required by the
implementation plan): `max_depth` (BFS levels, default 2), `max_nodes`
(distinct entities visited including the start node, default 50),
`max_edges` (relationship rows returned, default 200), and
`deadline_seconds` (wall-clock, default 2.0s). `TraversalResult.truncated`/
`truncated_reason` report which limit stopped early
(`"max_depth"`/`"max_nodes"`/`"max_edges"`/`"deadline"`), or `None` if the
reachable graph was exhausted naturally. Cycles terminate safely: a
previously-visited node is never re-queued onto the BFS frontier, so a
cyclic graph can only shrink toward an empty frontier, never loop. These
defaults are hardcoded `TraversalLimits`/`relationships.py` module
constants (`DEFAULT_MAX_DEPTH`, `DEFAULT_MAX_NODES`, `DEFAULT_MAX_EDGES`,
`DEFAULT_DEADLINE_SECONDS`) — **there is no `relationship_max_depth` or
`relationship_max_nodes` config.yaml key**; despite being named in the
implementation plan's configuration table, they were shipped as fixed
traversal-call defaults instead of operator-tunable settings.

**No automatic relationship extraction, and no relationship-assisted
recall, in this release.** `relationships.py` is explicitly **not** wired
into `get_tool_schemas()`/`handle_tool_call()` — the model cannot query or
mutate relationships at all — and, just as importantly, **the automatic
`prefetch()`/recall path never calls into `relationships.py` either.**
Relationships are populated by one deterministic, explicit path only: the
`hermes memory backfill-relationships` operator command (see [Operator
Maintenance Commands](#operator-maintenance-commands)), and are otherwise
only *read* by the optional [Local Diagnostic Viewer](#local-diagnostic-viewer)'s
`/entity` route. Consequently, **there is no `relationship_recall_enabled`
config key either** — the implementation plan named one for a feature
("gate relationship-assisted recall") that was not implemented in this
integration at all, so there is nothing for such a flag to gate. If
relationship-assisted recall is implemented in a future phase, expect a new
flag to be introduced alongside it; today, disabling relationships entirely
simply means never running `backfill-relationships`.

**Backfill heuristic** (`maintenance.backfill_relationships`): does not
re-run capture-time entity-extraction regexes. It reuses entities already
resolved and linked at fact-write time (the `fact_entities` join table) —
for every *active* fact with two or more distinct linked entities, every
unordered pair becomes a candidate `co_occurs_with` relationship, scoped to
that fact's namespace, with a fixed low confidence (`0.3`) and
`extraction_method="backfill_cooccurrence"`. Pairs are canonicalized as
`(min(entity_id), max(entity_id))`, so re-running the backfill after an
interruption upserts the same edge rather than creating a mirrored
duplicate.

**Health diagnostics detect orphaned relationship rows without repairing
them** — see [Operator Maintenance Commands](#operator-maintenance-commands)
below; `hermes memory health`/`verify` report orphans as a read-only issue,
they never mutate `entity_relationships`.

## Operator Maintenance Commands

All `hermes memory` subcommands below are **operator-only**: none are
wired into `get_tool_schemas()`/`handle_tool_call()`, none are reachable
from a gateway slash command or the Telegram/Slack command menus, and none
accept a raw chat namespace. Every command resolves its target database
through the same profile machinery as every other `hermes memory`
subcommand — the global `-p NAME`/`--profile NAME` flag (handled in
`hermes_cli/main.py` before any module import, so `HERMES_HOME` is already
correct by the time these commands run) selects a named profile; omitting
it uses the default profile. There is no way to point any of these
commands at "all profiles" or an arbitrary database path.

```bash
hermes memory health [--json] [--backup-path PATH]
hermes memory benchmark --gold-set PATH [--json]
hermes memory rebuild-vectors [--dry-run | --apply] [--json]
hermes memory rebuild-banks [--dry-run | --apply] [--json]
hermes memory backfill-relationships (--dry-run | --apply) [--json]
hermes memory verify [--json] [--backup-path PATH]
hermes memory restore --from PATH [--dry-run | --apply] [--allow-backup-warnings] [--json]
hermes memory viewer [--host HOST] [--port N] [--show-raw-identity]

hermes -p NAME memory <subcommand> ...   # named profile, any of the above
```

`health` and `benchmark` are documented in detail in
[Diagnostics](#diagnostics) above (`hermes memory health` / `hermes memory
benchmark`); they are read-only and covered there, not repeated here.

**`rebuild-vectors`** — `plugins/memory/holographic/maintenance.py`'s
`rebuild_vectors()`, wrapping `MemoryStore.rebuild_all_vectors(namespace=None)`
(already idempotent: rebuilding twice produces the same vectors both times,
so interrupting and re-running is always safe). `--dry-run` (the default —
`--dry-run` and `--apply` share one argparse destination, defaulting to
dry-run) opens the database `mode=ro` and only counts how many facts would
be processed; it performs zero writes. `--apply` creates and verifies a
backup first (aborting with no writes if verification fails), then
acquires the maintenance lock and recomputes every fact's HRR vector.
Result JSON: `ok`, `dry_run`, `operation`, `db_path`, `algorithm_version`,
`schema_version`, `exit_code`, `counts` (`facts_planned` in dry-run mode,
`facts_processed` after apply), `backup`, `issues`.

**`rebuild-banks`** — same dry-run/apply/backup/lock shape, via
`rebuild_banks()`, wrapping `MemoryStore.rebuild_bank` once per distinct
`(namespace, category)` pair with at least one non-superseded fact. This is
a full reconcile, not just a rebuild of banks that already have active
facts: it also deletes orphan `memory_banks` rows — banks whose
`(namespace, category)` no longer has any active fact behind it (e.g. a
pre-Phase-2 unscoped bank left behind once every fact in that pair was
superseded, removed, or migrated to a namespaced bank). Result `counts` in
dry-run mode: `pairs_planned`, `banks_to_rebuild`, `banks_to_delete`,
`banks_existing`, `banks_expected`. After apply: `pairs_rebuilt`,
`banks_deleted`. Idempotent: a second `--apply` reports `banks_deleted: 0`
once every orphan is gone, and leaves `hermes memory health` clean of
`BANK_COUNT_MISMATCH`.

**`backfill-relationships`** — unlike the two commands above, `--dry-run`
and `--apply` are **two independent flags, and neither defaults to true**:
the command refuses to run (`exit code 3`/`EXIT_CANNOT_RUN`, "requires
either --dry-run or --apply") unless you pass exactly one explicitly. Both
paths scan for the deterministic co-occurrence candidates described in
[Entity Relationships](#entity-relationships) above; `--apply` backs up and
verifies first, same as the rebuild commands. Result `counts`:
`candidates_planned`/`namespaces_affected` (dry-run) or
`relationships_written` (apply).

**`restore`** — see [Backup and Restore](#backup-and-restore) above for the
full contract (gates, dry-run diff shape, apply result shape, exit codes).
Unlike the other write commands, it takes `--from PATH` (required) rather
than operating on a fixed implicit target, and its JSON result isn't the
shared `MaintenanceResult` shape — it has its own richer `diff`/`gates`/
`plan`/`post_health` fields, since there's a destination database to
compare against and apply-blocking gates to report, not just planned
counts.

**`verify`** — a thin alias for `health`'s underlying checks
(`maintenance.verify()` calls `diagnostics.run_diagnostics()` directly), kept
as a separate command name because the implementation plan explicitly
allows "add `hermes memory verify` or reuse health consistently" — this
avoids a second, divergence-prone implementation of the same checks under a
different name.

**`viewer`** — starts the [Local Diagnostic Viewer](#local-diagnostic-viewer)
described below; see that section for its security model. `--port` defaults
to `8787`; `--host` defaults to `127.0.0.1` and only accepts
`127.0.0.1`/`localhost`/`::1`.

**Target confirmation.** None of these commands prompt interactively before
a write — `--apply` (or, for `backfill-relationships`, choosing `--apply`
over `--dry-run`) *is* the confirmation, since it is already an explicit,
deliberate flag the operator must type. In non-JSON mode, the exact
resolved database path and whether this is a dry run or an apply are
printed before any write is attempted; in both text and JSON mode, the
resolved `db_path` is always present in the final result payload too, so an
operator can verify what was touched even after the fact.

**Locking.** Every `--apply`/write path acquires a cross-process, exclusive,
non-blocking lock (`fcntl.flock(LOCK_EX | LOCK_NB)`) on a sidecar
`<db_path>.maintenance.lock` file before backing up or writing — the same
separate-lock-file convention `tools/memory_tool.py`'s own file lock already
uses, so the data file itself can still be atomically replaced by unrelated
code. A second concurrent maintenance invocation against the *same*
database fails immediately with a clear error (exit code `4`/
`EXIT_LOCKED`) rather than blocking; two profiles with two different
database files never contend for the same lock, since the lock path is
derived from the resolved `db_path`.

**Backup-before-write.** Every write path calls `backup.create_backup()`
and immediately `backup.verify_backup()` on the result, while already
holding the maintenance lock; if verification fails, the command aborts
with no writes and a nonzero exit code (`2`/`EXIT_ERRORS`). Maintenance
backups land under
`$HERMES_HOME/backups/holographic/maintenance/<namespace>/`, distinct from
the `fact_store {"action": "backup"}` directory used for manual chat-driven
backups.

**Stable exit codes** (`maintenance.py`): `0` = ok, `1` = reserved for a
future warnings-but-ok state (not currently emitted by any command in this
phase), `2` = errors found, `3` = cannot run (malformed input / database not
found / a required flag missing), `4` = another maintenance invocation
holds the lock for this database.

**Idempotent restart.** Every write operation above is a full rebuild from
current state, not an incremental delta, so interrupting any of them and
re-running with `--apply` again converges to the same end state rather than
compounding partial progress.

## Local Diagnostic Viewer

`hermes memory viewer` starts an optional, local-only, read-only HTTP
server for browsing Holographic memory — useful for debugging what got
captured/recalled without writing ad-hoc SQL. It is disabled unless an
operator explicitly runs this command; nothing else starts it, and no
normal Hermes startup path (CLI, gateway, `model_tools`, `hermes_cli.main`)
imports `plugins.memory.holographic.viewer` at all.

**Stdlib-only.** The module uses only `http.server`, `sqlite3`, `html`, and
`urllib.parse` — no new third-party dependency was added. (`fastapi`/
`uvicorn` are declared in `pyproject.toml` but are not imported by any
runtime code path in this repository; pulling in an unused ASGI stack for
one optional local tool was judged a worse trade than the stdlib, which is
already present everywhere Python runs.)

**Loopback-only; non-loopback is not supported at all.**
`create_server(host=..., ...)` only accepts `127.0.0.1`, `localhost`, or
`::1`. Anything else raises `NonLoopbackBindingNotSupported` *before any
socket is opened* — a deliberate hard refusal, not a flag an operator could
set to bind elsewhere: the implementation plan requires authentication and
CSRF protection before any non-loopback binding, and this phase adds
neither, so non-loopback binding simply isn't offered.

**Structurally read-only.** Every database connection is opened with
SQLite's `mode=ro` URI flag (the same technique `hermes memory health`
uses) — an accidental write would raise `sqlite3.OperationalError` rather
than silently succeeding. Independently, every HTTP route rejects every
non-GET method with `405 Allow: GET`; no route ever calls a
`MemoryStore` write method, executes a raw SQL fragment built from request
input (the one free-text filter, `q`, is always passed as a bound SQL
parameter), accepts a file path, or offers a restore action.

**Routes** (all GET-only):

| Route | Required params | Returns |
|---|---|---|
| `/` (also `/index`, `/index.html`) | — (`page`/`per_page` optional) | Namespace *selector* only — opaque digest, total/active/superseded fact counts, and a forgotten (tombstone) count per namespace. Never renders an individual fact; there is no "all namespaces" fact listing. |
| `/namespace` | `ns` (digest) | Paginated fact list for that one resolved namespace (id/status/category/trust/created/redacted content), plus its tombstone count. `q` optionally filters by a literal content substring. |
| `/fact` | `ns`, `id` | One fact's content (redacted), category, tags (redacted), trust score, confidence, source_type, extraction_method, created/updated timestamps, linked entities, and its supersession chain (ancestors/successor), bounded to 50 hops. |
| `/entity` | `ns`, `id` | One entity's name (redacted) and type, its namespace-scoped relationships, and a bounded BFS traversal (depth 2 / 50 nodes / 100 edges / 2s deadline) from it. |

A missing required parameter is a `400`; a `ns` digest or `id` that doesn't
resolve to real data in that namespace is always a `404` — **never** a
silent fallback to "no filter" or to another namespace's data. The digest
in `ns` is always re-derived from the actual namespaces present in the
database and matched by equality; a request can never supply a raw
namespace string to "unlock" a namespace it doesn't otherwise match.
Cross-namespace access (namespace B's fact/entity ID requested under
namespace A's digest) always 404s.

**Pagination.** Every list view (namespace selector, fact list,
relationship list, traversal) is capped at `MAX_PAGE_SIZE = 50` rows per
page regardless of what a client requests via `?per_page=`
(`DEFAULT_PAGE_SIZE = 25` when unspecified) — a request can never pull the
full table in one response.

**Opaque namespace labels by default; `--show-raw-identity` is a narrow,
explicit, server-start-only opt-in.** Namespace labels reuse the same
one-way SHA-1 digest `diagnostics.py`'s `_namespace_digest` already uses.
`--show-raw-identity` must be passed as a CLI flag *when starting the
server* — it cannot be requested per-visit via the URL — and even then it
only adds the raw namespace string next to its digest on the `/` selector
page. It never changes which routes exist, never disables redaction, and
never affects the read-only/loopback-only guarantees.

**Secret redaction.** Fact content, fact tags, and entity names are passed
through `redact_secret_shapes()` (`plugins/memory/holographic/__init__.py`)
before rendering — the exact same regex-based secret-shape detector the
capture-time write firewall already uses — as defense-in-depth for data
that predates that filter or was inserted directly against the store (e.g.
by a test or an import path).

**Minimized provenance.** Fact pages show `status`, `category`,
`trust_score`, `confidence`, `source_type`, `extraction_method`,
`created_at`/`updated_at` — **never** `fact_sources.chat_id`/`platform`/
`author` (raw identity fields), the on-disk database path, or any
environment value.

**Active/superseded/forgotten are visually distinct.** Active and
superseded facts get distinct colored status badges in every listing.
Forgotten facts have no retrievable content at all — `remove_fact` hard-
deletes the row — so they are represented honestly as an opaque tombstone
*count* in a visually distinct badge, never a fabricated fact-like entry.

## Retrieval Telemetry

Optional, **off by default**, content-free telemetry recording what
`FactRetriever.search()`'s scoring actually produced — intended to build a
dataset for a future, separately-reviewed offline evaluation of retrieval
weights. This phase is **observation-only**: nothing anywhere in this
module, `retrieval.py`, or any other code path reads a recorded event back
to adjust a scoring weight or threshold at runtime. The demo's
`learn_weights_from_feedback()` heuristic was deliberately **not** ported —
it cannot attribute a helpful/unhelpful signal to a specific retrieval
strategy and can amplify popularity feedback loops. Offline weight
*recommendations* built from this data remain an explicit future design,
not something implemented here.

**Schema** (`retrieval_events` table):

| Column | Type | Contents |
|---|---|---|
| `event_id` | TEXT (PK) | A random UUID, generated fresh per event — never derived from the query text (hashing the query could leak content via a targeted dictionary/collision search against known phrases). Also the handle `fact_feedback` uses to link feedback back to this row. |
| `namespace_digest` | TEXT | The same one-way SHA-1(namespace)[:12]-style digest `diagnostics.py` already uses for standard output — never the raw `platform:chat_id` string. |
| `fact_id` | INTEGER | A bare SQLite primary key — deliberately *not* digested, since it is already meaningless outside this one local database file and every other `fact_store` action (search/probe/list/history/...) already returns it verbatim. |
| `fts_score` / `jaccard_score` / `hrr_score` | REAL (nullable) | The three component scores `FactRetriever.search()` already computes in-memory. `hrr_score` is always `NULL` when NumPy is unavailable (see [NumPy Degradation](#numpy-degradation)). Numbers only — never the query or fact text that produced them. |
| `temporal_multiplier` / `trust_multiplier` | REAL (nullable) | The temporal-decay factor (`1.0` when decay is disabled) and the fact's trust score at retrieval time. |
| `final_rank` | INTEGER | 1-based position in the *returned*, post-limit result list — not the raw combined score — so telemetry reflects what the caller actually saw. |
| `helpful` | INTEGER (nullable: NULL/0/1) | Absent until an explicit `fact_feedback` call links to this event. |
| `algorithm_version` | TEXT | Mirrors `diagnostics.ALGORITHM_VERSION`/`benchmark.ALGORITHM_VERSION` (currently `"1"`). |
| `created_at` | TIMESTAMP | SQLite `CURRENT_TIMESTAMP` at insert time. |

**Never written, by design:** prompts, fact content/text, entity or person
names, chat IDs, raw namespace strings, or credentials. Only
`FactRetriever.search()` is instrumented — `probe`/`related`/`reason` score
purely by HRR similarity with no separable FTS/Jaccard component, so
inventing placeholder zero values for fields they don't actually compute
would misrepresent the schema; they are left uninstrumented (their internal
fallback to `search()` when HRR is unavailable is already covered by that
call).

**Configuration.** `retrieval_telemetry` (default `false`) must be set to
`true` to record anything at all — nothing is written while it is off, by
construction (every insert call site is conditioned on this flag).
`retrieval_telemetry_retention` (default `1000`, clamped to `[1, 100000]`
when set) bounds the table to a maximum row count: `prune_retrieval_events`
runs automatically after every recorded event and deletes the oldest rows
(by `created_at`, then `rowid` as a tiebreak) down to the configured cap.
This is a hard row-count cap, not age-based retention — deterministic and
directly testable independent of wall-clock time.

**Feedback linkage.** `fact_feedback {"action": "helpful"|"unhelpful",
"fact_id": ...}` looks up the most recent *not-yet-answered* event for that
`(fact_id, namespace)` pair (`helpful IS NULL`) and sets its `helpful`
column — it never overwrites an event that already has feedback, even if a
newer pending event for the same fact exists.

**Fail-safe: telemetry can never break retrieval.** Every call site wraps
telemetry recording in `try`/`except`, so a telemetry failure (locked
database, disk full, anything) never propagates out of or changes the
return value of a `search()`/`probe()`/etc. call — the retrieval result the
caller sees is identical whether or not telemetry succeeded.

**Profile-scoped.** `retrieval_events` lives in the same per-profile SQLite
file as every other table here (`store.py`'s `_SCHEMA`) — there is no
separate global telemetry store shared across profiles.

## Database Migrations and Schema Versioning

Three database generations are supported transparently, detected purely by
introspecting the actual on-disk schema (`MemoryStore._detect_schema_generation`)
— never by trusting the presence or absence of the version table alone,
since a genuinely old database predates that table's existence entirely:

- **`pre_pr2`** — the oldest shape: a global `UNIQUE(content)` constraint
  and none of the status/provenance columns (`status`, `superseded_by`,
  etc.) added later.
- **`pre_pr3`** — the status/provenance columns exist, but either the
  global `UNIQUE(content)` constraint is still present or the `namespace`
  column is still missing.
- **`current`** — neither legacy trait remains.
- **`fresh`** — no `facts` table at all yet (brand-new database file).

**Explicit version tracking.** A `schema_metadata(key, value)` table holds
`schema_metadata['schema_version']`, a single monotonically-increasing
integer stored as text; `CURRENT_SCHEMA_VERSION = "1"` is the first version
this plugin tracks explicitly (every pre-PR2/pre-PR3/pre-this-phase
database predates the constant and simply has no row — that absence alone
is never used to infer which generation it is, only the introspection
above is). `_finalize_schema_version()` writes the row only once a database
is confirmed `current`, and is a true no-op (zero writes) once the stored
value already matches — so re-opening an already-current database does not
register as a change on every single startup.

**Rebuild safety.** Where a migration requires a destructive table rebuild
(dropping the pre-PR3 global `UNIQUE(content)` constraint), the whole
rebuild runs inside one explicit transaction. Before that transaction
commits, `PRAGMA foreign_key_check` must report zero violations and a
facts/`facts_fts`/`fact_sources` row-count parity check must pass
(`MemoryStore._validate_facts_rebuild`); FTS triggers and existing indexes
are explicitly dropped and recreated bound to the rebuilt table (a rename
alone does not re-bind a trigger/index created against the old table name).
If validation fails, a `MigrationValidationError` is raised and the
transaction rolls back — **the original database is left exactly as it
was**, never partially migrated.

**Idempotency.** Every migration re-checks its own precondition on every
open (e.g. "does `facts` still have a content-alone unique index?") and is
a no-op once that precondition is already satisfied — reopening an
already-migrated database repeatedly produces no further changes, and an
interrupted migration (killed before commit) leaves the original,
untouched database to retry against on the next open, because nothing was
committed.

**Legacy tables are never silently dropped.** No migration path added by
this integration deletes `memory_banks` or `entity_relationships` rows; the
relationship-scope migration (`_migrate_entity_relationships`) only ever
creates/extends that table, defensively and idempotently, never removes
data from it.

**Downgrade is explicitly unsupported.** `schema_metadata` and
`entity_relationships` (and `retrieval_events`) are purely additive: older
plugin code has no SQL statement anywhere that references them, so
reopening a migrated database with *older* plugin code is safe — those
tables are silently ignored, exactly as any unknown table would be. The
reverse is **not** supported: there is no migration path, planned or
implemented, that converts a current-schema database back into a
pre-PR2/pre-PR3 shape. If you need to run older code against data created
by this version, keep a verified backup taken *before* upgrading and
restore from that instead of attempting an in-place downgrade.

## NumPy Degradation

A consolidated summary of how every NumPy-dependent feature degrades when
NumPy is unavailable (`hrr._HAS_NUMPY is False`) — see also the
NumPy-dependent behavior note under [Memory Tools](#memory-tools) for the
pre-existing `fact_store` action-level behavior.

| Feature | Behavior without NumPy |
|---|---|
| Scoped HRR banks | `rebuild_bank`/`rebuild_all_vectors` are no-ops (they check `self._hrr_available` first and return immediately) — no bank rows are written or read at all, scoped or legacy. `probe`/`related`/`reason` fall back to keyword search (`contradict` returns empty — pre-existing behavior, unchanged by this integration). |
| Fuzzy dedup consolidation | **Unaffected.** Fuzzy N-gram evidence is pure string/token processing with no HRR dependency; it behaves identically with or without NumPy. |
| Batch ingestion | **Unaffected** for the write/consolidation logic itself; any bank rebuild it would trigger is simply skipped, same as any other single-item write. |
| Entity relationships | **Unaffected.** Relationship storage/traversal is plain SQL with no HRR dependency. |
| Retrieval telemetry | Still records events, but `hrr_score` is always `NULL` in every row (the retriever never computes an HRR component without NumPy); `fts_score`/`jaccard_score`/`temporal_multiplier`/`trust_multiplier`/`final_rank` are populated normally. |
| Local viewer | **Unaffected.** The viewer is pure SQLite introspection; it never imports or depends on NumPy/HRR. |
| Benchmark (`hermes memory benchmark`) | Reports `numpy_available: false` and an `hrr_only`/`default` strategy breakdown reflecting the degraded (FTS+Jaccard-only) scoring — this is intentional, accurate reporting, not a bug. |
| Health (`hermes memory health`) | Unaffected — integrity/FK/FTS-parity/orphan checks are plain SQL; HRR-vector-shape checks simply report "no vectors to check" rather than failing. |

## Configuration Defaults and Rollback Reference

This section consolidates the operator-facing config keys and manual
procedures added across Phases 2–8, and states exactly how to roll each one
back. See [Configuration Reference](#configuration-reference) above for the
pre-existing PR1–PR3 keys.

| Key | Default | Rollback |
|---|---|---|
| `fuzzy_dedup_enabled` | `true` | Set to `false` to return to pure token-overlap consolidation (the pre-Phase-3 behavior). Already-consolidated facts are unaffected; this only changes how *future* captures are matched. |
| `fuzzy_dedup_threshold` | unset (uses the calibrated `0.4` constant) | Unset it (or remove the key) to return to the calibrated default; this has no effect while `fuzzy_dedup_enabled: false`. |
| `retrieval_telemetry` | `false` | Already off by default. Setting it back to `false` stops new events from being recorded immediately; existing rows are untouched (delete the `retrieval_events` table's contents manually via a backup/restore cycle if a clean slate is wanted — there is no `fact_store` action for this, by design, same as every other operator-only maintenance operation). |
| `retrieval_telemetry_retention` | `1000` | Lower it to shrink the table on the next recorded event (pruning is lazy, triggered by writes); there is no immediate-prune command. |

**Features that are shipped but have no config rollback switch, by
design** (the implementation plan's own configuration table named these as
flags; the shipped code either made the behavior unconditional or never
wired a disable path, as noted in each feature's own section above):

- **Scoped HRR banks** — always on, structural (see [Namespace-Scoped HRR
  Banks](#namespace-scoped-hrr-banks)). There is no `scoped_hrr_banks` key.
  The functional equivalent of "rolling back" is that any missing/absent
  bank already falls back to the pre-existing per-fact scoped scoring —
  never to a cross-namespace legacy bank — so there is nothing unsafe left
  exposed even without a flag.
- **Relationship-assisted recall** — never implemented in this integration
  (see [Entity Relationships](#entity-relationships)); there is no
  `relationship_recall_enabled` key because there is no such recall path to
  gate. Relationships exist only via the explicit `backfill-relationships`
  operator command and the read-only viewer.
- **Relationship traversal bounds** (`relationship_max_depth`,
  `relationship_max_nodes`) — fixed `TraversalLimits` defaults in
  `relationships.py` (depth 2, nodes 50, edges 200, 2s deadline), not
  config.yaml keys.

**Operator-procedure rollbacks** (not config keys — manual steps):

- **Stop using scoped banks entirely** — there is no flag; rely on the
  automatic per-fact scoped-scoring fallback described above. Never point
  scoped retrieval at a legacy `cat:<category>` bank — no code path does
  this.
- **Delete/rebuild derived data after backup** — `memory_banks` and
  `retrieval_events` are both derived, non-authoritative data (facts remain
  the source of truth); after taking and verifying a backup, they may be
  cleared and rebuilt with `hermes memory rebuild-banks --apply` (banks) or
  by restoring a pre-telemetry backup (telemetry events — there is no
  dedicated "clear telemetry" command).
- **Relationship tables stay additive on downgrade** — reverting to older
  plugin code leaves `entity_relationships` in place, unread and unwritten
  by that older code; no data loss, no broadcast of unscoped data into
  per-chat namespaces.
- **Never roll back by disabling profile isolation or deleting tombstones**
  — `profile_isolation: false` and manually deleting `forgotten_facts` rows
  are both explicitly out of scope as "rollback" procedures for *any*
  feature in this integration; see [Profile, Chat, and Database
  Isolation](#profile-chat-and-database-isolation) and the backup/restore
  tombstone-honoring behavior above.

## Memory Tools

| Tool | Description |
|------|-------------|
| `fact_store` | 14 actions (see below) |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) — actions `helpful`/`unhelpful`, requires `fact_id` |

Automatic capture (above) writes facts without any tool call; `fact_store`
is for explicit operations the model (or you, via chat) choose to perform —
adding a fact outside of what capture would extract, searching/browsing,
structured HRR reasoning, diagnostics, and backup/share.

### `fact_store` actions

| Action | Description |
|--------|-------------|
| `add` | Store a new fact (rejected if it fails the write firewall) |
| `search` | FTS5 + trust-weighted keyword search |
| `probe` | HRR compositional query for facts where an entity plays a structural role |
| `related` | HRR query for facts structurally connected to an entity |
| `reason` | Multi-entity HRR intersection query |
| `contradict` | Find potentially contradictory fact pairs (entity overlap + content divergence) |
| `update` | Edit a fact's content/tags/category/trust |
| `remove` | Delete a fact (tombstoned, so a later restore can't silently reactivate it) |
| `list` | Browse active facts, optionally filtered by category/trust |
| `stats` | Fact/entity counts, by-category breakdown, active/superseded counts |
| `history` | Superseded facts and what replaced them (the only action that surfaces them) |
| `diagnostics` | Sanitized operational counters (recall/capture/error counts) — never fact content |
| `backup` | Snapshot the live database (checksum + restricted permissions) |
| `share` | Copy one of your own active facts into shared memory, if enabled and allowlisted |

Common arguments across actions: `content` (for `add`), `query` (for
`search`), `entity`/`entities` (for `probe`/`related`/`reason`), `fact_id`
(for `update`/`remove`/`share`), `category` (`user_pref`/`project`/`tool`/
`general`), `tags`, `trust_delta` (for `update`), `min_trust` (filter,
default `0.3`), and `limit` (default `10`, at most `100`).

**Examples:**

Add a fact:
```json
{"action": "add", "content": "I use PostgreSQL for my Solana analytics project.", "category": "project"}
```

Search:
```json
{"action": "search", "query": "database preference", "limit": 5}
```

List active facts in a category:
```json
{"action": "list", "category": "project"}
```

View superseded history:
```json
{"action": "history"}
```

Check diagnostics:
```json
{"action": "diagnostics"}
```

Create a manual backup:
```json
{"action": "backup"}
```

**NumPy-dependent behavior.** `add`/`search`/`update`/`remove`/`list`/
`stats`/`history`/`diagnostics`/`backup`/`share` are pure SQLite/FTS5 and
behave identically with or without NumPy. `probe`/`related`/`reason` fall
back to keyword search (`search()`) when NumPy is unavailable. `contradict`
is the one exception: without NumPy it returns an **empty list**, not a
keyword-search fallback. `search()` itself also runs in a degraded
(FTS+Jaccard only, no HRR term) scoring mode without NumPy, by automatically
redistributing `hrr_weight` to `0`.

## Security and Input Provenance

Security claims below are separated into what Holographic enforces
directly, what Hermes's calling code is responsible for, and what remains
a heuristic or an open limitation — don't conflate these.

### Write firewall

**Enforced directly by Holographic**, on every write path (explicit
`fact_store add`, automatic capture, `auto_extract`, imports, and
`share`): recognized credential patterns, recovery/seed phrases, suspicious
instruction-like phrasing, and encoded blobs are rejected before storage.
This is a pattern-based filter — it rejects *recognized* shapes of secret
or malicious content, not every possible secret or malicious instruction
that could theoretically be phrased.

### Recall firewall

**Enforced directly by Holographic**: recalled facts are wrapped in a
`<memory-context>` fence and explicitly labeled as recalled data, not new
user input or system instructions, so a model is not meant to treat stored
text as an instruction with authority over its behavior. This reduces, but
does not make impossible, a prompt-injection attempt that was itself
captured as a stored "fact" — it is a labeling/framing mitigation, not a
content-understanding defense.

### Input provenance

```text
Only genuine user-authored content may be passed as user_content.

Tool results, retrieved documents, and assistant-generated summaries
must not be reclassified as direct user assertions.
```

This is a **contract enforced by Hermes Agent's calling code** (`cli.py`,
`gateway/run.py`, `run_agent.py`), not by Holographic itself — Holographic
has no provenance parameter on `sync_turn()` to check against. Hermes
upholds it today via `persist_user_message` (an explicit clean-text
override, separate from whatever synthetic prefix the model sees) and via
`skip_memory=True` for cron/subagent/background-review contexts. Verified
current behavior:

- CLI model-switch and skill-reload notes are excluded from memory input (`cli.py`).
- Gateway model-switch and skill-reload notes are excluded (`gateway/run.py`).
- Voice-transcription prefixes are excluded (`cli.py`).
- Cron jobs and delegated subagents skip automatic memory synchronization entirely (`skip_memory=True`).
- Background-review agents skip automatic synchronization while retaining intended explicit memory-store access (`skip_memory=True` plus a manually-shared `_memory_store`).
- Tool-call results are never concatenated into `user_content`; they travel as separate `role: "tool"` messages.

Full call-path details, code patterns, what to avoid, and the exact
input/output strings from the regression test suite are in
[`docs/input-provenance.md`](docs/input-provenance.md) and
[`tests/agent/test_memory_input_provenance.py`](../../../tests/agent/test_memory_input_provenance.py).

## Known Limitations

- **No tool-output provenance channel.** `sync_turn(user_content,
  assistant_content, ...)` has no parameter distinguishing genuine
  user-authored text from tool output, retrieved documents, or other
  untrusted material relayed as if it were user text — the plugin trusts
  the caller completely (see [Input provenance](#input-provenance) above).
  The write firewall's role-markup check happens to reject some
  impersonation shapes, such as content starting with a literal `"USER:"`
  prefix, but that's a side effect of unrelated chat-role/tool-call markup
  detection, not a dedicated defense against this specific risk.
- **Token-overlap consolidation matching is approximate** — see
  [Automatic Capture, Consolidation, and Forgetting](#automatic-capture-consolidation-and-forgetting).
- **`contradict` has no non-NumPy fallback** — see [Memory Tools](#memory-tools).
- **`agent_context`-keyed write gating is not an actively exercised runtime
  mechanism today** — see [Profile, Chat, and Database Isolation](#profile-chat-and-database-isolation).
  `skip_memory=True` is what actually isolates cron/subagent/background-review.
- Holographic does not independently authenticate message origin; its
  provenance correctness depends entirely on the calling agent preserving
  message roles and source attribution before calling
  `sync_turn()`/`prefetch_all()`.
- **Fuzzy duplicate evidence does not catch deep-synonym paraphrases** —
  see [Fuzzy Duplicate Evidence in Consolidation](#fuzzy-duplicate-evidence-in-consolidation).
  This is a deliberate conservative tradeoff (prefer a safe miss over a
  wrong merge), not an oversight.
- **No automatic relationship extraction or relationship-assisted recall
  exists in this release** — see [Entity Relationships](#entity-relationships).
  Relationships are populated only by the explicit `hermes memory
  backfill-relationships` operator command and read only by the optional
  viewer; the automatic `prefetch()` recall path never consults them.
- **Scoped HRR banks have no configuration rollback switch** — see
  [Configuration Defaults and Rollback Reference](#configuration-defaults-and-rollback-reference).
  Namespace-scoping the bank key is always-on, structural behavior.

**Future improvement:** introduce structured provenance metadata at the
agent-to-memory integration boundary itself, with explicit rules for which
source types are eligible for automatic capture, rather than relying
solely on caller discipline.
