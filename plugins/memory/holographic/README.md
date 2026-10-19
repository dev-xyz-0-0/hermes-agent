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

No settings are currently purely experimental beyond `backup_enabled`'s
reserved-for-scheduling note above — every other key already has a live
effect in the current implementation.

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
`consolidation_superseded`, `consolidation_forgotten`, `sqlite_errors`),
and `by_category` (only present when `diagnostics_include_content: true`).
It never includes raw fact content, and works even against a down/erroring
store (so the error counters themselves stay reachable). Set
`diagnostics_enabled: false` to get a minimal `{"diagnostics_enabled": false}`
stub instead (counters still aren't collected in that case). See the
[Quick Start example](#5-check-diagnostics) for a full sample response.

## Backup and Restore

`fact_store {"action": "backup"}` snapshots the live database using
SQLite's online backup API (safe against a concurrently open WAL
connection — no file copy, no pausing writers) to
`$HERMES_HOME/backups/holographic/<namespace>/memory-<timestamp>.db`, with a
SHA-256 checksum sidecar and `0600` permissions. `backup_retention` prunes
older backups automatically after each manual backup.

**Restore is deliberately NOT a `fact_store` tool action** — it's exposed
only as `HolographicMemoryProvider.restore_from_backup(path)`, a method for
an operator (or a future `hermes memory restore` CLI command, which does
**not** currently exist) to call directly, never from chat. A
prompt-injected or confused model triggering a restore could silently
reactivate facts the user explicitly forgot; that decision belongs to a
human.

**Operator procedure:**

1. **Create a backup** via chat or the memory tool: `fact_store {"action": "backup"}`. Note the path it reports under `$HERMES_HOME/backups/holographic/<namespace>/`.
2. **Verify its integrity** by checking the SHA-256 checksum sidecar written alongside it — `restore_from_backup()` does this automatically, but you can verify independently before relying on an old backup.
3. **Stop processes accessing the database** — close the live store (end the Hermes/gateway process using it) before restoring; restore does a file-level swap, which an open WAL connection could otherwise corrupt.
4. **Invoke the supported restore interface** — there is no CLI command for this today; call `HolographicMemoryProvider.restore_from_backup(path)` directly (e.g. from a short Python script or REPL run by an operator with filesystem access).
5. **Verify restored data** — after restore completes, run `fact_store {"action": "stats"}` or `{"action": "diagnostics"}` against the restored database and spot-check expected facts with `{"action": "list"}` / `{"action": "search"}`.
6. **Forgotten-fact tombstones are handled automatically** — restore reads every `forgotten_facts` tombstone (recorded whenever a fact is explicitly removed, across all namespaces) *before* touching the live file, and re-deletes any matching row the backup would otherwise reintroduce, so a backup taken before an explicit forget can't bring that fact back.
7. **If restoration fails** — `restore_from_backup()` verifies the backup's checksum and runs `PRAGMA integrity_check` twice (on the backup itself, then again on the staged copy) *before* touching anything live, and keeps a timestamped safety copy of the pre-restore database indefinitely. If a failure is reported, the live database should be untouched; recover by inspecting that safety copy and the error raised, rather than retrying blindly.

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

**Future improvement:** introduce structured provenance metadata at the
agent-to-memory integration boundary itself, with explicit rules for which
source types are eligible for automatic capture, rather than relying
solely on caller discipline.
