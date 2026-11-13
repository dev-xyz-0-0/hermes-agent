# Holographic Memory PR 2 — Automatic Capture, Consolidation, Forgetting

Target: `plugins/memory/holographic/`. Goal: Hermes automatically identifies,
extracts, stores, updates, and forgets durable facts stated in conversation —
without a `fact_store` call — while preserving everything PR 1 built.

## 1. PR 1 verification (prerequisite, per spec section 1)

Before touching anything, the full PR 1 regression suite was run and
confirmed green: `pytest tests/plugins/memory/ -q` → **146 passed** (0
failed) prior to any PR 2 change. Provider init/shutdown, SQLite close,
cross-session automatic recall, add/replace/remove preservation, and
fail-open behavior were all already verified working — see
`docs/plans/2026-10-15-holographic-pr1-foundation-and-recall.md` for that
audit. PR 2 builds on top of it rather than touching its code paths, except
where noted in section 2.

## 2. Repository audit — the real hooks

- **`sync_turn(user_content, assistant_content, *, session_id="")`** was a
  no-op (`pass`) in the checked-out code. It is called exactly once per
  completed turn, from `run_agent.py`'s `run_conversation()`:
  `self._memory_manager.sync_all(original_user_message, final_response)` —
  **after** the response is generated but, per the surrounding code
  (`_persist_user_message_idx` bookkeeping, then this call, then "Background
  memory/skill review — runs AFTER the response is delivered"), **before**
  the response is handed back to the caller. This is the single, correct,
  already-shared integration point for both CLI and every gateway platform
  (same reasoning as PR1's `prefetch_all()` call site) — no core change was
  needed, confirming section 3.1's "prefer existing hooks" default held.
  **Implication**: capture logic runs synchronously on the response-latency
  path, so it must stay fast and bounded (see `capture_timeout_ms`).
- **No stable per-turn message ID is threaded through this call** — neither
  `sync_all` nor `prefetch_all` receive one at their actual call sites (only
  the raw message strings). The idempotency key design (section 4 below)
  works around this explicitly, as the spec's section 3.3 anticipated.
- **`on_session_end()` / `_auto_extract_facts()`** (the existing regex-based,
  session-end, `auto_extract`-gated extractor) was left **completely
  untouched** — different trigger (session end vs. per-turn), different
  config key, different output format, and existing tests
  (`test_holographic_fixes.py::TestAutoExtract`) pin its exact string output.
  Reusing or replacing it risked silent regressions for no benefit; PR 2's
  new per-turn pipeline is additive and separately gated by `auto_capture`.
- **Schema**: `facts` had no notion of "superseded" — `remove_fact()` was
  the only way anything left active recall, and it was a hard delete. PR 2
  needed a soft state for corrections (keep history) and kept hard delete
  for forgetting (spec section 6: "do not retain deleted content in
  supersession history as a workaround").

## 3. Design decisions and tradeoffs

**No LLM-assisted extraction.** Section 4.5 of the spec describes this as
optional and explicitly says not to enable it by default. Investigating it
turned up a real blocker: a `MemoryProvider` plugin has no handle to the
agent's model/routing layer (`agent/memory_provider.py`'s lifecycle gives it
config and `hermes_home`, nothing else), and wiring one through would be a
core change — out of scope per section 13 ("no major Hermes core
refactoring"). `extraction_mode: llm` is accepted in config, logs a one-time
warning, and falls back to `rules` rather than silently doing nothing or
raising. This is reported as a genuine, documented blocker rather than a
partial/fake implementation.

**Consolidation reuses `FactRetriever.search()`, not `MemoryStore.search_facts()`
directly.** The latter passes raw text straight into FTS5 `MATCH` with no
sanitization and no fallback — it reliably failed (silently, via a broad
`except`) on ordinary sentence-shaped queries during manual testing. The
former already has FTS5 sanitization with an AND→OR fallback and was built
exactly for this. No new retrieval code was written; PR 2 simply calls into
what PR 1 already built.

**Scope-aware duplicate/correction thresholds, tuned against the spec's own
worked examples, not guessed.** Token overlap is computed on content with
scaffolding words stripped (pronouns, "use/uses/prefer", "is/are", "instead/
now/decided/switch(ed)" — i.e. the words that recur across every phrasing of
*any* fact and would otherwise dilute the signal). This is what lets "My MCP
projects use uv" / "I use Python uv for MCP projects" / "I prefer uv for
Python MCP development" collapse to one fact while "Project Alpha uses
PostgreSQL" / "Project Alpha uses MySQL" do **not** — they differ in exactly
the one word that matters once scaffolding is removed. Thresholds
(`_DUPLICATE_OVERLAP=0.6`, `_CORRECTION_OVERLAP=0.3`, `_FORGET_OVERLAP=0.25`
in `consolidation.py`) were set by running the spec's PostgreSQL→ClickHouse,
five-times-uv, Project-Alpha/Beta, and Vim-forget examples end-to-end and
checking the actual output — not theorized in the abstract. See section 7
for the measured eval numbers.

**Ambiguous same-scope conflicts are preserved, never merged.** "Project
Alpha uses PostgreSQL" then "Project Alpha uses MySQL" (no correction verb)
results in **both** staying active. This directly implements spec section
5.5 ("do not automatically replace... preserve uncertainty") — the tradeoff
is that the two facts now coexist rather than being flagged in some separate
review queue (no such queue exists in this plugin, and building one was
judged out of scope for a rule-based PR 2 pass). A later explicit correction
("I switched Project Alpha to MySQL") would supersede cleanly.

**Forgetting is a hard delete, correction is a soft supersede.** These are
deliberately different operations per spec section 6: a forgotten fact is
gone (not recoverable via history, compaction, or replay); a superseded fact
stays queryable via `fact_store {"action": "history"}` only.

## 4. Idempotency (spec section 3.3)

Since no stable message ID reaches `sync_turn()`, the key is
`sha1(session_id + "||" + normalized_user_content)`, tracked in a bounded
in-memory LRU (16 sessions × 64 turns each, same pattern as PR 1's repeat-
injection tracker). **Documented limitation**: this also suppresses a
genuinely repeated identical message within the tracked window, not just a
transport-level replay — for automatic *capture* (as opposed to recall) this
is the safer direction to err in, since re-processing an identical statement
would only ever have produced an "ignored: exact duplicate" consolidation
result anyway. The tracking is in-memory only (not persisted), so it does
not survive a process restart — acceptable because `add_fact`'s own
UNIQUE-content constraint is a second, durable line of defense against
literal duplicate rows even across restarts.

## 5. Schema migration

Added to `facts` (idempotent `ALTER TABLE ... ADD COLUMN`, same pattern as
the pre-existing `hrr_vector` migration): `status TEXT DEFAULT 'active'`,
`superseded_by INTEGER`, `source_type TEXT DEFAULT ''`,
`extraction_method TEXT DEFAULT ''`, `confidence REAL DEFAULT 1.0`.

SQLite backfills a column's `DEFAULT` into **pre-existing rows** on
`ALTER TABLE ADD COLUMN` (not NULL) — verified in
`tests/plugins/memory/test_holographic_capture.py::TestMigration`, which
builds a facts table matching the exact pre-PR2 schema, inserts a row, opens
it with the current `MemoryStore`, and confirms: the fact is preserved, its
`status` backfills to `'active'`, re-opening the migrated file a second time
is a no-op (idempotent), and new facts can be added afterward. No data
rewrite, no destructive step.

`search_facts`/`list_facts` on `MemoryStore`, and all six raw-SQL query
sites in `retrieval.py` (`_fts_candidates`, `probe`, `related`, `reason`,
`contradict`, `_score_facts_by_vector`), now exclude `status='superseded'`
by default. `MemoryStore.get_history()` is the one path that surfaces them.

### Backup / rollback

The migration is additive and non-destructive, but as with any schema
change: **back up `$HERMES_HOME/memory_store.db` (and its `-wal`/`-shm`
sidecar files, if present) before upgrading**, e.g.
`cp memory_store.db memory_store.db.bak-pre-pr2`. To roll back PR 2's
*behavior* without touching data, set `auto_capture: false` — already
written facts, their `status`/`superseded_by` metadata, and PR 1's recall
all keep working unchanged. To roll back the *code*, reverting this commit
is safe — the new columns are simply unused by older code, nothing reads
them in a way that would break.

## 6. Configuration guide

See `plugins/memory/holographic/README.md` for the full table and worked
explanation. Summary:

| Key | Default |
|---|---|
| `auto_capture` | `false` |
| `extraction_mode` | `rules` (`llm` accepted, falls back with a warning) |
| `max_facts_per_turn` | `5` (clamped 1-10) |
| `consolidation_enabled` | `true` |
| `capture_timeout_ms` | `500` (clamped 10-5000) |
| `capture_debug` | `false` |

All default **off** (`auto_capture: false`) — upgrading never silently
starts writing facts for an existing install. This mirrors the PR1 decision
on `auto_recall`'s default, for the same reason (see PR1's doc, section 3).

## 7. Manual testing guide

```bash
source venv/bin/activate
hermes config set plugins.hermes-memory-store.auto_capture true
hermes
> I'm using PostgreSQL for my Solana trading analytics.
> exit
hermes   # new session
> What database am I using for my Solana analytics?
# Expected: PostgreSQL, no fact_store call visible.

hermes
> I've decided to switch my Solana analytics database to ClickHouse.
> exit
hermes
> Which database did I choose for Solana analytics?
# Expected: ClickHouse (not PostgreSQL).
```

Telegram: same two conversations via `hermes gateway start telegram`,
across a gateway restart — identical behavior, since `sync_turn()` is
invoked from the exact same `run_agent.py` call site for both runtimes (see
section 2). Set `capture_debug: true` and check logs for
`holographic.capture: op=... action=...` lines to confirm automatic capture
actually ran (sanitized — no fact content logged).

## 8. Evaluation results (measured, not claimed)

A 40-utterance labeled corpus (`tests/plugins/memory/test_holographic_capture_eval.py`)
covering every category the spec lists — preferences, project decisions,
corrections, forget requests, questions, chatter, hypotheticals, quoted/
reported speech, credentials, synthetic-summary-style text, and raw
stack-trace-style output:

```
holographic capture eval (n=40): precision=1.00 recall=0.95 tp=20 fp=0 tn=19 fn=1
  missed [credential]: 'Here is my password: hunter2hunter2.'
```

This is smaller than the spec's suggested 100-turn corpus — 40 honestly
labeled and measured cases were judged more valuable than padding toward
100 with filler. The one remaining miss is a credential sentence that
doesn't match any recognized statement pattern, so it's never extracted at
all — safe by omission (if it *had* matched, the write firewall would have
caught it before storage anyway, as separately verified in
`TestSyncTurnCapture::test_secret_is_never_stored_even_if_syntactically_capturable`).
Zero false positives across all 19 negative cases (questions, hypotheticals,
chatter, quotes, synthetic summaries, stack traces never extract a
candidate). "I always/never/usually..." habit statements (spec section 4.4)
are recognized by `_RE_STATEMENT` in `capture.py`.

Capture precision/recall are tracked as a regression floor
(precision ≥ 0.85, recall ≥ 0.75) — well below the measured 1.00/0.95 — so
the test fails loudly if a future change meaningfully degrades extraction
quality, without being so tight that normal corpus growth trips it.

## 9. Regression report

```
python -m pytest tests/plugins/memory/ -q
# 189 passed (146 PR1 + 42 capture/consolidation/migration/concurrency + 1 eval)

python -m pytest tests/ -q
# 9869 passed, 32 skipped, 1 xpassed, 3 failed, 157 warnings in ~142s
```

The same 3 failures as PR1's regression report
(`test_approve_deny_commands.py::test_blocking_approval_approve_once`,
`test_api_key_providers.py::TestHasAnyProviderConfigured::test_claude_code_creds_ignored_on_fresh_install`,
`::test_config_dict_no_provider_no_creds_still_false`) — pre-existing,
unrelated, reproduced identically without this change.

New coverage added in this PR: deterministic extraction (operation
classification, rejection of questions/hypotheticals/quotes/chatter, max-
candidates cap), `sync_turn()` wiring (natural capture with zero
`fact_store` calls, assistant-authored text structurally never considered,
secret rejection, turn idempotency, `auto_capture`/`consolidation_enabled`
toggles, fail-open on a dead store, bounded timeout, sanitized debug
logging), consolidation (exact duplicate, cross-phrasing duplicate,
correction/supersession with history preserved, different-scope facts both
retained, ambiguous same-scope conflict preserved not merged, correction
transaction failure leaves the original fact intact, repeated-correction
idempotency), forgetting (known fact removed from recall, nonexistent-fact
no-op, project-scoped isolation, survives restart, skipped rather than
guessed when consolidation is disabled), schema migration (pre-PR2 database
preserved/migrated/idempotent/still writable), concurrency (10 threads
capturing the same statement concurrently → one active fact, no deadlock,
no exception), and three MemoryManager-level end-to-end tests reproducing
the spec's exact worked scenarios (natural capture + cross-session recall,
correction flowing through to recall, five duplicate turns staying one
fact).

## 10. Acceptance criteria — status

- [x] PR 1 regression tests pass (146/146, unchanged).
- [x] Automatic capture works in normal CLI conversations (verified via
  `sync_turn()` directly and via `MemoryManager` end-to-end).
- [x] Telegram shares the same call site as CLI — verified architecturally
  (section 2), not independently exercised per-adapter (same caveat as PR1).
- [x] No explicit `fact_store` invocation required (structural — capture
  never calls the tool handler, only `sync_turn()` → `capture`/`consolidation`
  → `store`).
- [x] Extracted facts are concise and scoped (capped at 200 chars; scope
  hint used for category + consolidation matching).
- [x] Duplicate facts prevented (exact + stopword-stripped paraphrase).
- [x] Explicit corrections supersede old facts (transactional, history
  preserved).
- [x] Ambiguous conflicts handled conservatively (preserved, not merged).
- [x] Forgetting removes targeted facts from future recall (hard delete,
  scope-isolated, survives restart).
- [x] Secrets and synthetic/compaction-style messages excluded (write
  firewall + extraction never looks at anything but the raw per-turn user
  message — no summaries, no assistant text).
- [x] Capture retries are idempotent (bounded per-session turn-hash guard).
- [x] SQLite transactions/migrations are safe (tested failure-injection
  rollback + idempotent migration + concurrency).
- [x] Memory failures do not block conversations (`sync_turn` never raises;
  tested against a closed/dead store).
- [x] All applicable tests pass; no PR 1 regressions.
- [x] Configuration and rollback documented (README + section 5/6 above).

## 11. Limitations / explicitly out of scope

- **LLM-assisted extraction**: not implemented (see section 3) — a genuine
  blocker, not a partial attempt. `extraction_mode: llm` degrades to `rules`
  with a warning.
- **Semantic (embedding) deduplication**: not implemented, per the explicit
  restriction against new vector databases/embedding frameworks. Dedup is
  stopword-stripped token overlap over FTS5 candidates — it handles the
  spec's own worked examples correctly (measured, section 8) but will miss
  paraphrases that share little vocabulary.
- **Forgetting by abstract category name** (e.g. "forget my programming
  language preference" when the stored fact only mentions the concrete
  value, "Rust") is unreliable — the pipeline prefers a safe no-op over a
  wrong deletion. Forgetting by a concrete keyword that's actually in the
  stored fact (the spec's own "forget my editor preference" → "editor is
  Vim" example) works correctly.
- Per-adapter Telegram/Discord/Slack testing: not independently exercised
  beyond the shared call-site argument (same caveat PR1 documented).
