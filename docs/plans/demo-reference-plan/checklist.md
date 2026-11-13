# Holographic Demo Reference Integration — Checklist

Date: 2026-11-12

Plan: `docs/plans/demo-reference-plan/implementation-plan.md`

Use this checklist as the execution and review gate. Check an item only after
the implementation and its tests are present. Record exact test counts and any
intentional deviations in the relevant PR.

## 0. Scope and safety

- [x] Confirm `demo-reference/holographic-memory-main/` is reference-only and
      is not imported by production runtime code.
- [x] Confirm existing PR1–PR3 behavior is the compatibility baseline.
- [x] Confirm no feature makes memory global.
- [x] Confirm no feature disables or bypasses fail-closed gateway isolation.
- [x] Confirm no chat ID, profile name, or namespace is hard-coded.
- [x] Confirm no model tool accepts an arbitrary namespace.
- [x] Confirm restore, raw SQL, repair, and backfill remain unavailable to the
      model.
- [x] Confirm no change mutates past conversation messages or changes toolsets
      during a conversation.
- [x] Confirm all persistent paths use `get_hermes_home()` and all displayed
      paths use `display_hermes_home()` or profile helpers.
- [x] Confirm tests use temporary `HERMES_HOME` and never write to
      `~/.hermes/`.

## 1. Baseline audit

- [x] Run the existing Holographic test suite before implementation.
- [x] Record baseline pass/fail/skip/warning counts.
- [x] Record current facts/entity/source/bank/tombstone schema.
- [x] Record current indexes, foreign keys, and FTS triggers.
- [x] Snapshot current `fact_store` and `fact_feedback` tool schemas in tests.
- [x] Snapshot current provider configuration defaults in tests.
- [x] Add fixtures for unscoped CLI memory.
- [x] Add fixtures for two isolated gateway chats.
- [x] Add a threaded-chat namespace fixture.
- [x] Add a shared-memory namespace fixture.
- [x] Add NumPy-enabled and NumPy-disabled fixtures.
- [x] Add pre-PR2 and pre-PR3 database fixtures.
- [x] Add a current-schema fixture with active, superseded, and forgotten
      facts.
- [x] Add a deterministic benchmark gold set under `tests/fixtures/`.

### Baseline compatibility tests

- [x] Existing databases open without data loss.
- [x] Existing configuration loads unchanged.
- [x] Existing tool actions preserve response shapes.
- [x] CLI add/search/list/update/remove/history still work.
- [x] Automatic recall still works across fresh CLI sessions.
- [x] Automatic capture remains bounded and idempotent.
- [x] Gateway namespaces remain isolated.
- [x] Gateway sessions with missing chat identity still fail closed.
- [x] NumPy-unavailable mode retains FTS-only search.
- [x] Provider shutdown remains idempotent.

## 2. Phase 1 — Benchmark library

- [x] Create an importable benchmark module under the production plugin.
- [x] Do not import demo runtime modules.
- [x] Accept an explicit database/profile and gold-set path.
- [x] Add a read-only retrieval/evaluation mode with no counter mutations.
- [x] Compute Recall@1.
- [x] Compute Recall@5.
- [x] Compute mean reciprocal rank.
- [x] Report empty-result and query-error rates.
- [x] Report FTS/Jaccard/HRR component contributions.
- [x] Report temporal-decay variants.
- [x] Report P50/P95 latency.
- [x] Report namespace leakage count.
- [x] Include algorithm/schema versions in JSON output.
- [x] Ensure repeated deterministic runs produce stable metrics.

### Benchmark tests

- [x] Metrics match a small hand-calculated corpus.
- [x] Active facts are eligible and superseded facts are excluded.
- [x] Benchmarking does not increment retrieval counters.
- [x] Benchmarking does not change trust or feedback counters.
- [x] A deliberately broken fixture produces a leakage failure.
- [x] Normal fixtures report zero namespace leakage.
- [x] NumPy/no-NumPy capability reporting is accurate.
- [x] Malformed gold-set input fails with a stable safe error.
- [x] JSON output parses and contains the documented fields.

## 3. Phase 1 — Health diagnostics

- [x] Add a read-only diagnostics module.
- [x] Check SQLite integrity.
- [x] Check foreign keys.
- [x] Check required tables, columns, indexes, and triggers.
- [x] Check FTS/facts parity.
- [x] Detect orphaned fact-entity links.
- [x] Detect orphaned provenance rows.
- [x] Detect orphaned relationships.
- [x] Detect invalid or cyclic supersession chains.
- [x] Detect malformed/wrong-dimension HRR vectors.
- [x] Validate memory-bank counts and dimensions.
- [x] Validate status and trust-score constraints.
- [x] Validate a supplied backup manifest/checksum.
- [x] Avoid raw chat IDs and secret fact content in normal output.
- [x] Define stable diagnostic codes and exit statuses.

### Diagnostics tests

- [x] Healthy DB reports success.
- [x] Missing index/trigger fixtures report expected codes.
- [x] FTS parity mismatch reports the expected code.
- [x] Broken FK/orphan fixtures report expected codes.
- [x] Invalid supersession reference and cycle are detected.
- [x] Malformed vector and bank count mismatch are detected.
- [x] Diagnostics do not mutate the DB.
- [x] Diagnostics output redacts identity and secret-shaped content.
- [x] JSON output is stable and machine-readable.

## 4. Phase 1 — CLI wiring

- [x] Add `hermes memory health`.
- [x] Add `hermes memory benchmark`.
- [x] Support default and named profiles.
- [x] Support `--json` without decorative output.
- [x] Resolve profile paths through existing helpers.
- [x] Do not expose raw arbitrary namespace selection.
- [x] Do not add gateway/slash-command exposure.
- [x] Update help and autocomplete through the central command machinery where
      applicable.

### CLI tests

- [x] Help lists the intended subcommands.
- [x] Default profile targets the default HERMES_HOME.
- [x] Named profile targets the named profile only.
- [x] Invalid profile/path fails safely.
- [x] JSON mode has deterministic exit codes.
- [x] Telegram and Slack command menus remain unchanged.

## 5. Phase 2 — Namespace-specific HRR banks

- [x] Define a deterministic bank key for `(namespace, category)`.
- [x] Ensure raw chat IDs are not present in bank names.
- [x] Preserve an unscoped CLI bank identity.
- [x] Include only active facts in banks.
- [x] Pass namespace through bank rebuild APIs.
- [x] Update insert/update/supersede/forget paths.
- [x] Update restore/rebuild behavior.
- [x] Rebuild each affected bank once after batch commit.
- [x] Use scoped banks for scoped probe/reason paths where valid.
- [x] Never use legacy global banks for scoped retrieval.
- [x] Preserve per-fact scoped scoring as safe fallback.
- [x] Handle NumPy absence without errors.
- [x] Document legacy-bank cleanup behavior.

### Scoped-bank tests

- [x] Same category in two namespaces creates different bank keys.
- [x] Bank keys contain no raw identity.
- [x] Bank vectors include only their namespace.
- [x] Superseded facts are absent from rebuilt banks.
- [x] Forget removes the fact contribution.
- [x] Mutation rebuilds only affected namespace/category pairs.
- [x] Batch rebuild count is bounded to one per affected pair.
- [x] Scoped retrieval never consults a legacy global bank.
- [x] Rebuild preserves dimensions and fact counts.
- [x] Dimension migration is consistent.
- [x] FTS fallback works without NumPy.

## 6. Phase 3 — Fuzzy consolidation evidence

- [x] Port/adapt deterministic character N-gram similarity.
- [x] Keep it in consolidation, not `MemoryStore.add_fact()`.
- [x] Reuse current normalization and scope logic.
- [x] Compare only active same-namespace candidates.
- [x] Combine token and N-gram evidence conservatively.
- [x] Define named, documented thresholds.
- [x] Add strategy/score diagnostics without breaking existing result fields.
- [x] Keep corrections explicit and unambiguous.
- [x] Preserve ambiguous facts rather than merging them.
- [x] Run security rejection before similarity evaluation.
- [x] Gate rollout with configuration if required.

### Fuzzy consolidation tests

- [x] Exact duplicate remains ignored.
- [x] Durable-fact paraphrases consolidate.
- [x] Different critical values remain separate.
- [x] Explicit correction supersedes one clear target.
- [x] Ambiguous correction supersedes nothing.
- [x] User and project scopes do not cross-match.
- [x] Two namespaces do not cross-match.
- [x] Unicode and punctuation normalization is deterministic.
- [x] Oversized input remains bounded.
- [x] Secret/instruction-shaped content is rejected before comparison.
- [x] Threshold boundary cases are pinned.
- [x] Evaluation corpus meets documented precision/recall targets.

## 7. Phase 4 — Transactional batch ingestion

- [x] Define `BatchResult` and per-item result structures.
- [x] Require an already resolved namespace.
- [x] Add atomic and dry-run modes.
- [x] Add bounded batch and item sizes.
- [x] Reuse validation, normalization, consolidation, and tombstone policy.
- [x] Record provenance for each successful mutation.
- [x] Add transaction-aware store primitives/context manager.
- [x] Remove nested commits from the batch execution path.
- [x] Roll back facts, entities, links, sources, relationships, and tombstones
      together.
- [x] Rebuild affected banks only after successful commit.
- [x] Keep batch ingestion non-model-callable initially.
- [ ] Migrate built-in import only after compatibility tests pass. (Not
      migrated this phase — see PR notes: judged riskier to prove
      byte-for-byte equivalence than the task budget allowed; batch API
      added standalone, `import_builtin_memories` left as-is, its existing
      tests still pass unmodified.)

### Batch tests

- [x] Successful atomic batch commits all associated rows.
- [x] Mid-batch failure rolls back every mutation.
- [x] Dry-run performs zero writes.
- [x] Every per-item status is covered.
- [x] Provenance is complete.
- [x] Same text in different namespaces is allowed.
- [x] Same text in one namespace consolidates.
- [x] Tombstone/reintroduction policy is preserved.
- [x] Excessive batch/item input is rejected safely.
- [x] Concurrent batches do not corrupt SQLite.
- [x] Bank rebuild count is bounded.
- [x] Built-in-memory import retains prior behavior.

## 8. Phase 5 — Scoped relationships

- [x] Decide additive-column versus new-table migration after schema fixture
      testing. (New table: production had no `entity_relationships` table at
      all — confirmed by grepping store.py/retrieval.py/__init__.py — so this
      is a fresh additive table, not a column added to an existing one. It
      includes `namespace` inline from creation, plus a defensive idempotent
      migration path for the hypothetical case of a differently-shaped
      pre-existing table — see `MemoryStore._migrate_entity_relationships`.)
- [x] Add namespace to relationship ownership.
- [x] Add a composite namespace/source/target/type unique index.
- [x] Add extraction method, confidence, and timestamps.
- [x] Optionally link relationships to source facts.
- [x] Preserve legacy relationships as unscoped only.
- [x] Create a focused `relationships.py` service.
- [x] Implement add/upsert/remove/list operations.
- [x] Implement bounded breadth-first traversal.
- [x] Add depth, node, edge, and deadline limits.
- [x] Invalidate/remove derived edges after forget.
- [x] Recompute/invalidate stale edges after supersession.
- [x] Apply existing shared-memory allowlists to shared traversal.
- [x] Keep relationship mutations out of model tools initially.

### Relationship tests

- [x] Relationship uniqueness is namespace-scoped.
- [x] Identical entity names in two chats do not leak edges.
- [x] Direction and relationship type are preserved.
- [x] Upsert does not duplicate edges.
- [x] Strength/confidence are clamped.
- [x] Traversal respects depth/node/edge/deadline limits.
- [x] Cycles terminate safely.
- [x] Forget invalidates derived edges.
- [x] Superseded facts do not contribute stale active edges.
- [x] Shared traversal follows enablement and category allowlists.
- [x] Migration does not broadcast legacy edges into chat namespaces.
- [x] Health diagnostics detect orphan edges without repairing them.

## 9. Phase 6 — Maintenance commands

- [x] Add `hermes memory rebuild-vectors`.
- [x] Add `hermes memory rebuild-banks`.
- [x] Add `hermes memory backfill-relationships --dry-run`.
- [x] Add explicit `--apply` behavior.
- [x] Add `hermes memory verify` or reuse health consistently. (Implemented
      as a thin alias: `maintenance.verify()` calls
      `diagnostics.run_diagnostics()` directly and `cmd_verify` is a
      near-duplicate of `cmd_health` under the `verify` name — same checks,
      same JSON schema/exit codes, no separate check logic. See
      `plugins/memory/holographic/maintenance.py`'s module docstring for
      the rationale: the plan explicitly allows "reuse health
      consistently" and a literal second implementation would only risk
      drift between two things that must always agree.)
- [x] Resolve default/named profiles safely.
- [x] Acquire a maintenance lock for writes. (New: no cross-process lock
      existed for this plugin before this phase —
      `maintenance.maintenance_lock()` adds a non-blocking
      `fcntl.flock(LOCK_EX | LOCK_NB)` on a `<db>.maintenance.lock` sidecar
      file, following the same separate-lock-file convention already used
      by `tools/memory_tool.py`'s `MemoryStore._file_lock`.)
- [x] Create and verify a backup before apply operations.
- [x] Display exact target before confirmation. (`--apply` itself is the
      confirmation — see `_print_maintenance_target` in
      `hermes_cli/memory_setup.py`; there is no additional interactive
      prompt, and `db_path` is always present in the JSON result too.)
- [x] Support interruption and idempotent restart.
- [x] Avoid printing fact content or raw identities.
- [x] Define stable JSON output and exit codes. (0 ok, 1 reserved for a
      future warnings-but-ok state — not currently emitted by any Phase 6
      path, 2 errors, 3 cannot-run/malformed input, 4 lock held by another
      process. See `maintenance.py`'s `EXIT_*` constants.)
- [x] Keep commands unavailable through gateways/model tools.

### Maintenance tests

- [x] Dry-run changes no files or rows.
- [x] Apply refuses to run without required confirmation/flag.
- [x] Apply aborts when backup verification fails.
- [x] Lock prevents unsafe concurrent maintenance.
- [x] Interrupted operation can restart safely.
- [x] Repeated rebuild is idempotent.
- [x] Named profile operation cannot touch another profile.
- [x] Output contains no secrets or raw chat identity.

## 10. Database migrations

- [x] Add internal schema version metadata. (`schema_metadata` table +
      `CURRENT_SCHEMA_VERSION` in `plugins/memory/holographic/store.py`;
      versioning scheme documented at the constant's definition.)
- [x] Detect pre-PR2 schema. (`MemoryStore._detect_schema_generation`.)
- [x] Detect pre-PR3 global-unique schema. (same method.)
- [x] Detect current schema. (same method.)
- [x] Make new migrations idempotent. (`_finalize_schema_version` is a
      true no-op once the version row matches; `_migrate_drop_global_
      content_unique` re-checks `_has_unique_on_content_alone()` on every
      open and is a no-op once it is gone.)
- [x] Preserve FTS triggers and foreign keys during rebuilds. (triggers
      and the pre-existing `idx_facts_trust`/`idx_facts_category`/
      `idx_fact_sources_fact_id`/namespace indexes are explicitly dropped
      and recreated bound to the new table — see the fix for the
      RENAME-keeps-old-index/trigger-binding pitfall in
      `_migrate_drop_global_content_unique`.)
- [x] Validate FK/FTS parity before commit. (`_validate_facts_rebuild`:
      `PRAGMA foreign_key_check` plus facts/`facts_fts`/`fact_sources`
      row-count parity, run before `self._conn.commit()`.)
- [x] Leave original DB usable on migration failure. (explicit `BEGIN`
      wraps the whole rebuild — see the fix below for why this was
      required — so any exception, including a
      `MigrationValidationError` from the validation gate, rolls back the
      entire rebuild and leaves the original `facts`/`fact_entities`/
      `fact_sources` tables and data untouched.)
- [x] Do not silently delete legacy banks or relationships. (no code path
      added this phase deletes `memory_banks` or `entity_relationships`
      rows; the relationships migration only ever creates the table empty
      on a database that never had it — see the relationship-scope
      migration tests.)
- [x] Document downgrade behavior. (docstring on the `schema_metadata`
      table in `_SCHEMA`: both it and `entity_relationships` are additive
      and never queried by older code, so older code tolerates them;
      downgrading a database's *content* back to a pre-PR2/pre-PR3 shape
      is explicitly not supported or attempted.)

### Migration tests

All in `tests/plugins/memory/test_holographic_migrations.py`, reusing the
Phase 0 `pre_pr2_db`/`pre_pr3_db`/`current_schema_mixed_status_db` fixtures
from `tests/plugins/memory/conftest.py`.

- [x] Fresh DB creates latest schema.
- [x] Pre-PR2 fixture migrates.
- [x] Pre-PR3 fixture migrates.
- [x] Current DB migration is a no-op/idempotent.
- [x] Facts and categories survive.
- [x] Active/superseded state and revision links survive.
- [x] Provenance survives.
- [x] FTS search works after migration.
- [x] Tombstones survive.
- [x] Relationship scope migration is correct.
- [x] Injected migration failure preserves the original DB.
- [x] Reopen after successful migration remains healthy.

## 11. Phase 7 — Optional local viewer

- [ ] Keep viewer dependencies optional.
- [ ] Bind to loopback by default.
- [ ] Disable viewer by default.
- [ ] Make initial viewer read-only.
- [ ] Require explicit profile selection.
- [ ] Require explicit namespace selection.
- [ ] Avoid an all-namespaces default view.
- [ ] Redact secret-shaped fact content.
- [ ] Minimize displayed provenance.
- [ ] Distinguish active/superseded/forgotten states.
- [ ] Show revision and relationship provenance safely.
- [ ] Enforce pagination and query limits.
- [ ] Expose no raw SQL, restore, file access, or mutation endpoint.
- [ ] Require authentication and CSRF protection before any future write or
      non-loopback mode.

### Viewer tests

- [ ] Default bind address is loopback.
- [ ] Mutation HTTP methods/endpoints are absent or rejected.
- [ ] Namespace selection is explicit.
- [ ] Cross-namespace access is rejected.
- [ ] Pagination/query limits are enforced.
- [ ] Secrets and raw identities are absent from rendered output.
- [ ] Normal Hermes startup does not import optional viewer dependencies.

## 12. Phase 8 — Retrieval telemetry

- [ ] Do not copy demo adaptive-weight heuristic directly.
- [ ] Define content-free retrieval event schema.
- [ ] Include component scores, multipliers, rank, feedback, and algorithm
      version.
- [ ] Use opaque namespace/fact identifiers.
- [ ] Exclude prompts, fact text, names, chat IDs, and credentials.
- [ ] Keep telemetry disabled by default or explicitly opt-in.
- [ ] Enforce bounded retention.
- [ ] Keep telemetry profile-scoped.
- [ ] Do not change weights during an active conversation.
- [ ] Defer learned-weight rollout to a separate reviewed design.

### Telemetry tests

- [ ] Event schema contains no content or raw identity.
- [ ] Retention bound is enforced.
- [ ] Profiles remain isolated.
- [ ] Disabled telemetry writes nothing.
- [ ] Helpful/unhelpful feedback links to the correct opaque event.
- [ ] Telemetry failures do not break retrieval.

## 13. Documentation

- [ ] Update `plugins/memory/holographic/README.md`.
- [ ] Document scoped banks and legacy-bank behavior.
- [ ] Document fuzzy dedup semantics and limitations.
- [ ] Document batch limits and atomicity.
- [ ] Document relationship ownership and traversal bounds.
- [ ] Document health/benchmark/rebuild/backfill commands.
- [ ] Document profile selection and backup requirements.
- [ ] Document NumPy degradation.
- [ ] Document viewer security model if implemented.
- [ ] Document telemetry privacy/retention if implemented.
- [ ] Document migration and downgrade limitations.
- [ ] Document configuration defaults and rollback switches.
- [ ] State that demo-reference code is not imported at runtime.

## 14. Required validation per phase

- [ ] Activate `venv` before running Python.
- [ ] Run new phase-specific tests.
- [ ] Run `python -m pytest tests/plugins/memory/ -q`.
- [ ] Run `python -m pytest tests/agent/test_memory_provider.py -q`.
- [ ] Run memory provider release/shutdown tests.
- [ ] Run gateway session-boundary tests.
- [ ] Run CLI tests when CLI code changes.
- [ ] Run gateway tests when namespace/identity code changes.
- [ ] Run `python -m pytest tests/ -q` before merge.
- [ ] Run `git diff --check`.
- [ ] Inspect the dirty worktree and preserve unrelated user changes.
- [ ] Record exact pass/fail/skip/warning counts.

## 15. Final compatibility and acceptance gate

- [ ] CLI search works.
- [ ] CLI list works.
- [ ] CLI automatic recall works across new sessions.
- [ ] Gateway add/search/list work with a valid chat identity.
- [ ] Multi-user sessions with missing identity fail closed.
- [ ] Profile isolation remains intact.
- [ ] Thread isolation remains intact.
- [ ] Shared-memory controls remain intact.
- [ ] No scoped operation uses a global HRR bank.
- [ ] No new operation bypasses capture/write security filters.
- [ ] No forgotten fact is reactivated by migration, batch import, rebuild, or
      restore.
- [ ] Existing tool schema and response contracts remain compatible.
- [ ] Existing configuration remains valid.
- [ ] Existing databases migrate idempotently without data loss.
- [ ] Benchmark/diagnostic paths do not mutate production memory.
- [ ] Maintenance operations require explicit operator intent and backup.
- [ ] Full test suite passes.
- [ ] Documentation and rollback instructions are complete.
