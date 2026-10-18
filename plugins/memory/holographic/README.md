# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval.

**Features:**
- **Automatic recall** — relevant stored facts are injected before each turn with no `fact_store` call needed (see [Automatic recall](#automatic-recall)).
- **Automatic capture, consolidation, and forgetting** — durable facts stated in conversation are extracted, deduplicated, corrected, and forgotten on request, fully local and deterministic (see [Automatic capture, consolidation, and forgetting](#automatic-capture-consolidation-and-forgetting)).
- **Profile / chat isolation** — one gateway process serving many chats keeps each chat's memory separate by default (see [Profile / chat isolation](#profile--chat-isolation)).
- **Shared memory** — explicit, read-only, allowlisted cross-chat sharing (see [Shared memory](#shared-memory-explicit-read-only-allowlisted)).
- **Diagnostics** — sanitized operational counters via `fact_store` (see [Diagnostics](#diagnostics)).
- **Backup and restore** — checksum-verified SQLite snapshots with deletion-aware restore (see [Backup and restore](#backup-and-restore)).

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
| `extraction_mode` | `rules` | `rules` is local/deterministic; `hybrid` uses rules first and an auxiliary-model fallback for eligible user statements. Legacy `llm` values map to `hybrid` |
| `hybrid_capture_timeout_ms` | `5000` | Maximum auxiliary-model time for one hybrid fallback (clamped 100-30000 ms) |
| `max_facts_per_turn` | `5` | Max candidate facts extracted from one user turn (clamped 1-10) |
| `consolidation_enabled` | `true` | Dedupe/supersede/forget via consolidation; if `false`, captured facts are inserted as-is and forget requests are skipped (never guessed at) |
| `capture_timeout_ms` | `500` | Soft deadline for processing one turn's capture candidates (clamped 10-5000) |
| `capture_debug` | `false` | Log sanitized capture diagnostics (operation, action, fact IDs) — never fact content |
| `profile_isolation` | `true` | Scope facts by chat/profile identity (see below). Default **true** — unlike the flags above, "off" here means a real cross-chat data leak, not a conservative default |
| `diagnostics_enabled` | `true` | Track operational counters for `fact_store {"action": "diagnostics"}` — counts only, never fact content |
| `diagnostics_include_content` | `false` | Include a category breakdown in diagnostics output (still never raw fact text) |
| `shared_memory_enabled` | `false` | Allow read-only recall from an explicit shared namespace, for allowlisted categories only |
| `shared_memory_categories` | *(empty)* | Comma-separated category allowlist for shared memory (both reading it and the `share` action writing to it) |
| `backup_enabled` | `false` | Reserved for future automated backup scheduling — the manual `fact_store {"action": "backup"}` action works regardless |
| `backup_retention` | `14` | Keep the N most recent backups per namespace after each manual backup (`0` = keep all) |

### Profile / chat isolation

**The problem this fixes**: Hermes's own "profile" concept (`hermes -p <name>`,
a separate `$HERMES_HOME` per CLI invocation) was already fully isolated —
different processes, different `memory_store.db` files. The real gap was
inside a single long-running **gateway** process (Telegram, Discord, Slack,
WhatsApp, Signal, Home Assistant): every chat/user it served got its own
`HolographicMemoryProvider` instance, but every one of those instances
resolved to the *same* `$HERMES_HOME`, and therefore the *same* SQLite file
— with no query-level scoping. `chat_id`/`user_id` were captured only as
provenance metadata, never used to filter recall or capture. Five Telegram
chats talking to one gateway process shared one `facts` table.

**The fix**: every fact row now has a `namespace` column. For known
multi-tenant platforms (`telegram`, `discord`, `slack`, `whatsapp`, `signal`,
`homeassistant`) with a resolvable `chat_id`, the namespace is
`"{platform}:{chat_id}[:{thread_id}]"`; every read/write (recall, capture,
consolidation, `fact_store`, `on_memory_write` mirroring) is scoped to it.
CLI and other single-tenant-per-process contexts (cron, subagents) keep the
original unscoped (`''`) namespace — **zero behavior change for them**.

**Fail closed, never fall back**: if a multi-tenant platform session has no
resolvable `chat_id` (a runtime bug or an unusual integration), the provider
sets `_isolation_blocked = True` and every memory operation — `prefetch`,
`sync_turn`, every `fact_store`/`fact_feedback` call — becomes a safe no-op
(or returns an error for tool calls) rather than falling back to the shared/
unscoped bucket. A sanitized warning is logged once.

**To opt back into the old (shared) behavior** — not recommended for
multi-tenant deployments — set `profile_isolation: false`.

### Shared memory (explicit, read-only, allowlisted)

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

### Diagnostics

`fact_store {"action": "diagnostics"}` returns a sanitized operational
snapshot — recall/capture/consolidation/error counters, active/superseded
fact counts, the resolved namespace, and which features are enabled. It
never includes raw fact content, and works even against a down/erroring
store (so the error counters themselves stay reachable). Set
`diagnostics_enabled: false` to get a minimal `{"diagnostics_enabled": false}`
stub instead (counters still aren't collected in that case).

### Backup and restore

`fact_store {"action": "backup"}` snapshots the live database using SQLite's
online backup API (safe against a concurrently open WAL connection — no file
copy, no pausing writers) to
`$HERMES_HOME/backups/holographic/<namespace>/memory-<timestamp>.db`, with a
SHA-256 checksum sidecar and `0600` permissions. `backup_retention` prunes
older backups automatically after each manual backup.

**Restore is deliberately NOT a `fact_store` tool action** — it's exposed
only as `HolographicMemoryProvider.restore_from_backup(path)`, a method for
an operator (or a future `hermes memory restore` CLI command) to call
directly, never from chat. A prompt-injected or confused model triggering a
restore could silently reactivate facts the user explicitly forgot; that
decision belongs to a human. Restore is **deletion-aware**: it reads every
`forgotten_facts` tombstone (recorded whenever a fact is explicitly removed,
across all namespaces) *before* touching the live file, and re-deletes any
matching row the backup would otherwise reintroduce — so restoring a backup
taken before an explicit forget can't bring that fact back. It also: verifies
the backup's checksum and runs `PRAGMA integrity_check` twice (on the backup
itself, then again on the staged copy) before touching anything live; keeps
a timestamped safety copy of the pre-restore database indefinitely; and
requires the caller to close the live store first (restore does a file-level
swap, which an open WAL connection could otherwise corrupt).

### Automatic capture, consolidation, and forgetting

When `auto_capture: true`, `sync_turn()` — the same once-per-turn post-response
hook used by both CLI and gateway runtimes — runs a conservative, local,
rule-based pipeline (`capture.py` + `consolidation.py`, no LLM calls, no new
vector database):

1. **Extract**: deterministic patterns recognize explicit first-person
   statements, decisions, corrections ("switched X to Y", "no longer use
   X", "I don't use X anymore"), and forget requests ("forget...", "don't
   remember..."). Questions,
   hypotheticals ("we're evaluating...", "for example..."), and quoted/
   reported speech are rejected outright — nothing is captured from them.
   With `extraction_mode: hybrid`, a non-question user message that produces
   no rule candidate is sent to the configured auxiliary text model on a
   background worker. Its JSON output is untrusted candidate data and still
   passes every check below. Assistant responses are never extraction input.
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
cost and may finish shortly after the response is displayed. If no auxiliary
provider is available, output is malformed, or the deadline expires, capture
fails closed and diagnostics record the reason. The old `llm` value is a
deprecated alias for `hybrid`.

### Importing built-in memory

Existing `MEMORY.md` and `USER.md` entries are not silently copied during an
upgrade. Preview an idempotent, profile-scoped migration with:

```bash
hermes memory import --from builtin --provider holographic
```

The preview lists proposed, duplicate, and rejected entries without writing.
Apply exactly that migration explicitly with `--apply`. Imported entries pass
the same write firewall, retain their user/general category, and record
`builtin_import` provenance.

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
| `fact_store` | 14 actions (see below) |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |

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

All actions except `add`/`search`/`list`/`stats`/`history`/`diagnostics`/`backup`/`share` require numpy for full HRR behavior and fall back to keyword search otherwise. **Restore is intentionally not an action here** — see [Backup and restore](#backup-and-restore) for why.

### Profile Isolation

Holographic memory uses database-level isolation for Hermes profiles.

Each profile requiring independent memory must use a separate resolved SQLite database path, typically through a separate `HERMES_HOME` and profile-specific database configuration.

Within a database:

- Supported messaging platforms use platform/chat-based namespaces.
- CLI, cron, and subagent contexts use the shared default namespace (`""`).
- `agent_identity` does not participate in namespace resolution.
- The `profile_isolation` configuration does not provide profile-level separation when multiple profiles share the same database.

**Security requirement:** Profiles that must not share memory must never point to the same SQLite database.

This includes all associated SQLite WAL and SHM files.

Cross-profile memory sharing is possible only by intentionally configuring profiles to use a common database or through a separately implemented sharing mechanism.

Do not rely on `agent_identity` or `profile_isolation=true` to enforce isolation between profiles sharing one database.

### Known limitations

**No tool-output provenance channel.** `sync_turn(user_content,
assistant_content, ...)` has no parameter distinguishing genuine
user-authored text from tool output relayed as if it were user text —
callers are responsible for ensuring `user_content` is actually what the
user said. The write firewall's role-markup check (`_fact_problem`/
`_recall_problem`) happens to reject some impersonation shapes, such as
content starting with a literal `"USER:"` prefix, but that's a side effect
of unrelated chat-role/tool-call markup detection, not a dedicated defense
against this specific risk.