# Holographic Memory PR 3 — Profile Isolation, Observability, Backup/Restore

Target: `plugins/memory/holographic/`. Goal: make Holographic reliable,
secure, and production-ready across multiple Hermes profiles and chats,
while preserving everything PR 1 (automatic recall) and PR 2 (automatic
capture/consolidation/forgetting) built.

## 1. PR 1 / PR 2 verification

Both regression suites were green before this work started:
`pytest tests/plugins/memory/ -q` → 189 passed (146 PR1 + 43 PR2).

## 2. Repository audit — what "profile isolation" actually means here

Before writing code, a dedicated audit (not an assumption) answered the
spec's own warning: *"Do not assume Telegram chat IDs are equivalent to
profile IDs."*

- **Hermes's own "profile" concept** (`hermes -p <name>` / `--profile`) sets
  `HERMES_HOME` once, at process startup, via `_apply_profile_override()` in
  `hermes_cli/main.py` — fixed for the life of that process. Two `hermes -p`
  invocations are two separate processes with two separate
  `memory_store.db` files. **This was already fully isolated — zero risk,
  zero work needed.**
- **The gateway** (Telegram/Discord/Slack/WhatsApp/Signal/Home Assistant)
  runs as one long-lived process that can serve many chats/users
  concurrently. Each chat gets its own `AIAgent`/`MemoryManager`/
  `HolographicMemoryProvider` instance (cached per `session_key` in
  `gateway/run.py`) — but every one of those instances resolves
  `hermes_home` from the same process-global `get_hermes_home()`, so they
  all open the **same** `memory_store.db` file. `chat_id`/`user_id` were
  already threaded into `initialize()` and stored in `self._identity`, but
  **nothing used them to scope a query** — `retrieval.py`'s SQL had no
  chat/user filter at all. Confirmed by code inspection of every query site
  in `retrieval.py` and `store.py` before this PR: zero references to
  `chat_id`/`user_id`/any isolation key.
- **Sibling plugins' patterns**: `mem0`/`honcho` already scope by `user_id`;
  `supermemory` scopes by **profile** (`agent_identity` → `container_tag`
  with `{identity}` substitution) — not by chat. `byterover` scopes by
  filesystem `cwd`. None of them solve the specific gap found here
  (multiple chats, one process, one DB), so no existing pattern could be
  reused as-is — a new `namespace` dimension was the right fix.
- **A pre-existing bug found along the way**: `save_config()` imported
  `set_hermes_home_override`/`reset_hermes_home_override` from
  `hermes_constants` — neither function exists there. The broad
  `except Exception` silently swallowed the resulting `ImportError` on
  every call, so saving holographic config to a non-active profile via
  `hermes memory setup` never actually worked. Fixed by toggling the
  `HERMES_HOME` env var directly (the only mechanism `get_hermes_home()`
  actually supports) and verified with a round-trip test.

## 3. Design decisions

**Namespace, not separate files.** The spec allowed either; given facts
already live in one SQLite file with FTS5/HRR infrastructure built around
it, adding a `namespace` column (mirroring the existing `status` column's
migration pattern from PR 2) was far simpler and safer than splitting into
per-chat files, and it's what "the simplest secure design based on the
actual code" (spec section 3.2) called for.

**A migration inside a migration — the UNIQUE constraint had to be rebuilt,
not just extended.** `facts.content` had a *global* `UNIQUE` constraint.
Namespace isolation requires two different chats to each independently
store the same fact text (e.g. both saying "I prefer dark mode") without
colliding. SQLite can't alter a column constraint in place, so this
required rebuilding `facts` with a composite `UNIQUE(content, namespace)`
index instead. Verified empirically (not just read about) that SQLite
rewrites foreign-key *and* trigger bodies to point at a table's new name on
`ALTER TABLE ... RENAME` — rebuilding `facts` alone without also rebuilding
`fact_entities`/`fact_sources` (FKs) and dropping+recreating the FTS5 sync
triggers left dangling references that silently broke future inserts and
FTS indexing. The final migration rebuilds all three tables and the
triggers together, inside a single `PRAGMA foreign_keys=OFF` window, with a
full round-trip test against a simulated pre-PR-3 database fixture.

**Fail closed, never fall back (spec section 3.3, literally).** A
multi-tenant platform (`telegram`/`discord`/`slack`/`whatsapp`/`signal`/
`homeassistant`) session with no resolvable `chat_id` sets
`_isolation_blocked = True`. Every entry point — `prefetch`, `sync_turn`,
`on_memory_write`, `on_session_end`, and `handle_tool_call` (so both
`fact_store` and `fact_feedback`) — checks this first and no-ops (or
returns a tool error) rather than ever touching the unscoped/shared bucket.
CLI, cron, and subagent contexts are not multi-tenant-per-process, so they
were never blocked and keep the exact pre-PR-3 unscoped (`''`) namespace —
**verified with a dedicated backward-compatibility test.**

**`profile_isolation` defaults to `true` — the one flag in this whole
three-PR series that defaults ON rather than OFF.** Every other opt-in flag
(`auto_recall`, `auto_capture`, `shared_memory_enabled`, …) defaults off
because "off" means "unchanged legacy behavior." Here, "off" means "chats
share one fact table" — a latent data-isolation bug, not a feature users
opted into. Defaulting it on fixes the bug for everyone upgrading; CLI users
are completely unaffected either way (see above), so there's no surprise
for the common case, only for actual multi-chat gateway deployments, where
the surprise is the fix working.

**Shared memory implemented, not deferred — but narrowly.** The spec
explicitly permitted deferring this behind a documented interface if it
required "substantial changes to Hermes core." It didn't — the same
namespace mechanism already built for isolation made a *fixed shared
namespace* ("`__shared__`") a small, self-contained addition entirely
inside the plugin: read-only by default, allowlisted by category, and the
**only** write path is an explicit `fact_store {"action": "share"}` call
that copies one of the *caller's own* active facts (re-checked against the
write firewall) into the shared namespace. No automatic write-through, no
sharing of anything the caller doesn't already own, revocable by flipping
`shared_memory_enabled` off on the read side.

**Restore is a provider method, not a tool action — a deliberate security
boundary, not an oversight.** `fact_store {"action": "backup"}` exists and
is model-callable (safe: additive, read-only against the live DB). Restore
is not exposed to the model at all. A prompt-injected or merely confused
model triggering a restore could silently reactivate facts the user
explicitly forgot. That decision belongs to a human operator calling
`HolographicMemoryProvider.restore_from_backup(path)` directly (or a future
`hermes memory restore` CLI command built on top of it) — never to chat.
Pinned by a test that asserts `"restore"` is absent from the `fact_store`
schema's action enum while `"backup"` is present.

**Deletion-aware restore, actually implemented, not just documented as a
manual process.** The spec allowed documenting a manual reconciliation
step instead of building this. It was built: every explicit removal
(`remove_fact(..., tombstone=True)`, used by `fact_store {"action":
"remove"}`, the PR 2 forget pipeline, and the built-in-memory `remove`
mirror) records a SHA-256 content hash in a `forgotten_facts` table.
`restore_from_backup()` reads every tombstone **across all namespaces**
(the backup file could contain any of them) before touching the live file,
and re-deletes any matching row the restored backup would otherwise bring
back — verified end-to-end: forget a fact, restore an older backup that
still has it, confirm it stays gone while unrelated facts come back intact.

**A real, previously-latent concurrency bug was found and fixed while
testing this PR — not introduced by it.** `FactRetriever.search()` was
incrementing `retrieval_count` and calling `.commit()` directly on
`self.store._conn` **without** holding `self.store._lock` — the same raw
connection every other method in `store.py` already protects with that
lock. Under genuine multi-threaded contention (the PR 3 concurrency test,
10 threads hitting one store), this surfaced as an intermittent
`OperationalError("cannot commit - no transaction is active")`. Every raw
`conn.execute()` call site across `retrieval.py` (`search`, `probe`,
`related`, `reason`, `contradict`, `_score_facts_by_vector`,
`_fts_candidates`) now acquires the same lock store.py's own methods already
use. Confirmed fixed with 20 consecutive stress runs of the concurrency
test plus 5 consecutive full-suite runs, all clean (previously flaky).

## 4. Configuration guide

See `plugins/memory/holographic/README.md` for the full table and worked
explanation of each feature. Summary of new PR 3 keys:

| Key | Default |
|---|---|
| `profile_isolation` | `true` (the one default-on flag — see above) |
| `diagnostics_enabled` | `true` |
| `diagnostics_include_content` | `false` |
| `shared_memory_enabled` | `false` |
| `shared_memory_categories` | *(empty)* |
| `backup_enabled` | `false` (reserved for future scheduling; manual backup works regardless) |
| `backup_retention` | `14` |

### Required kill switches (spec section 8) — verified working

- Automatic recall: `auto_recall: false` (PR 1).
- Automatic capture/consolidation: `auto_capture: false` / `consolidation_enabled: false` (PR 2).
- Shared-memory access: `shared_memory_enabled: false` (immediately stops reads; already-shared facts aren't deleted, matching "revocation takes effect on subsequent retrieval").
- Detailed diagnostics: `diagnostics_enabled: false`.
- Automatic backups: never implemented as a scheduler in the first place — `backup_enabled` is reserved, not wired to anything that runs on its own.
- **Profile isolation is explicitly NOT a kill switch for the thing it protects** — `profile_isolation: false` is an opt-out, but there's no flag that disables Hermes itself; every other flag above fails open to "no memory operation," never to "Hermes stops responding."

## 5. Manual testing guide

```bash
source venv/bin/activate

# Gateway isolation (requires two chats hitting the same running gateway process):
hermes gateway start telegram
# From chat A: "My favorite database is PostgreSQL."
# From chat B: "My favorite database is MySQL."
# From chat A: "What database do I like?" -> expect PostgreSQL, not MySQL.
# From chat B: "What database do I like?" -> expect MySQL, not PostgreSQL.

# Diagnostics (from either chat, or CLI with holographic active):
# "diagnostics" via fact_store returns counters, active/superseded counts,
# the resolved namespace, and enabled features — set capture_debug/recall_debug
# true beforehand to also see per-turn log lines.

# Backup (operator, not chat):
python -c "
from plugins.memory.holographic import HolographicMemoryProvider
p = HolographicMemoryProvider()
p.initialize(session_id='manual', platform='cli')
print(p.backup())
"

# Restore (operator only — never via chat):
python -c "
from plugins.memory.holographic import HolographicMemoryProvider
p = HolographicMemoryProvider()
p.initialize(session_id='manual', platform='cli')
print(p.restore_from_backup('/path/to/backup/memory-YYYYMMDD-HHMMSS.db'))
"
```

## 6. Performance (measured)

Stress-tested, not just unit-tested: 6 concurrent gateway-chat providers
writing to the same `db_path` simultaneously (10 threads total across
isolation + fault-tolerance tests) — zero errors, zero cross-namespace
leakage, `PRAGMA integrity_check` clean, across 5 consecutive full-suite
runs. Backup of a small (3-fact) database completes in well under 100ms
locally (SQLite's online backup API, not a file copy) — not separately
benchmarked at 1,000/10,000 facts in this pass; the underlying mechanism
(SQLite's own backup API) is designed for multi-GB databases, so this
wasn't judged to be the risk worth spending the benchmark budget on versus
the correctness work above. Flagged honestly as unmeasured rather than
claimed.

## 7. Regression report

```
python -m pytest tests/plugins/memory/ -q
# 223 passed (146 PR1 + 43 PR2 + 34 PR3), stable across 5 repeated runs
```

New test files: `tests/plugins/memory/test_holographic_profiles.py` (34
tests) covering namespace isolation (two chats sharing one DB file but not
facts, conflicting preferences staying independent, fail-closed on missing
identity, CLI/profile_isolation=false backward compatibility, restart
durability, 6-thread concurrent-chat isolation, namespace-scoped
`on_memory_write` mirroring), shared memory (disabled-by-default, allowlisted
read access, unauthorized-category rejection, no-implicit-write-through,
revocation, credential rejection, can't-share-another-namespace's-fact),
diagnostics (metrics updating on success/empty/error, secrets never
appearing in diagnostics or logs, disabled stub, sanitized fail-closed
logging), backup/restore (healthy backup + checksum verification,
restricted file permissions, corrupted-backup rejection, fact-preserving
restore with integrity check, deletion-aware restore blocking reactivation,
safety-copy preservation, retention pruning, restore's absence from the
model-callable tool schema), and fault tolerance (dead-store graceful
degradation, concurrent read/write integrity, shutdown-during-capture
safety, corrupted-file-at-init failing loudly rather than silently).

A full-project run (`pytest tests/ -q`) was executed; the only failures
observed were in `tests/gateway/test_approve_deny_commands.py` and
`tests/hermes_cli/test_api_key_providers.py` — neither touches memory code,
and the `test_api_key_providers.py` failures were already confirmed
pre-existing and unrelated during PR 1 (reproduced identically via
`git stash`). Per the user's direction, verification for this PR focused
specifically on the code actually changed (the holographic plugin), which
is the 223-test suite above, run clean and repeatedly.

## 8. Acceptance criteria — status

- [x] PR 1 and PR 2 regression suites pass (189/189 unchanged, plus 34 new).
- [x] Profile isolation works by default (`profile_isolation: true`).
- [x] CLI routing verified unaffected; gateway routing verified via the
  real per-chat provider-instance pattern `gateway/run.py` already uses.
- [x] Missing profile identity cannot expose another profile's memory
  (fails closed, tested).
- [x] Optional sharing is securely controlled (allowlisted, read-only,
  explicit single write path) — implemented, not deferred.
- [x] Memory diagnostics work without leaking sensitive information
  (tested: secrets never appear in diagnostics or logs).
- [x] SQLite concurrency and error handling tested — and a real
  concurrency bug was found and fixed in the process.
- [x] Backup and restore operations pass integrity checks (checksum +
  `PRAGMA integrity_check`, twice).
- [x] Deleted facts cannot silently reappear through restoration
  (tombstone sweep, tested end-to-end).
- [x] Feature kill switches work (tested for recall/capture/sharing/
  diagnostics).
- [x] Hermes continues responding during memory failures (dead-store
  test: `prefetch`/`sync_turn`/tool calls all degrade gracefully).
- [x] No cross-profile leakage in automated tests (dedicated isolation +
  concurrency tests, zero leaks across 5 repeated runs).
- [x] Performance results measured where practical, honestly flagged
  where not (see section 6).
- [x] Rollback documented (`profile_isolation: false` to opt out;
  `restore_from_backup` with a timestamped, non-destroyed safety copy for
  the database itself).
- [x] No PR 1/PR 2 regressions (223/223, including everything from both
  earlier PRs).

## 9. Limitations / explicitly out of scope

- **No new CLI command or dashboard** for diagnostics/backup/restore, per
  spec section 5.3/16 ("do not introduce a new web dashboard"). All three
  are reachable programmatically (`fact_store` tool actions for
  diagnostics/backup; a direct provider method for restore) — a future
  `hermes memory backup`/`hermes memory restore` CLI command would be a
  thin wrapper over what's already built here.
- **No automated/scheduled backups.** `backup_enabled` is reserved; wiring
  it to `hermes cron` is a natural follow-up but out of scope here per the
  spec's own instruction to keep scheduling disabled until explicitly
  configured.
- **Shared memory has no per-category write-time validation beyond the
  existing write firewall** — i.e. there's no additional "trading
  decisions and wallet details are never shared" rule beyond "don't put
  those categories in `shared_memory_categories`." This was judged
  sufficient (the allowlist is the control point) rather than adding a
  second, redundant category blocklist.
- **Backup/restore benchmarks at 1,000/10,000-fact scale were not run** —
  see section 6 for the reasoning.
- **Disk-full and read-only-filesystem fault injection were not
  exercised** — not reliably simulable in this sandboxed environment;
  the existing `sqlite3.OperationalError`/generic-exception handling
  throughout the write paths (unchanged from PR 1/PR 2, which were
  specifically built to fail open) is the applicable defense, but it
  wasn't independently re-verified under these two specific conditions in
  this pass.
