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
- [ ] Detect orphaned relationships.
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

- [ ] Define `BatchResult` and per-item result structures.
- [ ] Require an already resolved namespace.
- [ ] Add atomic and dry-run modes.
- [ ] Add bounded batch and item sizes.
- [ ] Reuse validation, normalization, consolidation, and tombstone policy.
- [ ] Record provenance for each successful mutation.
- [ ] Add transaction-aware store primitives/context manager.
- [ ] Remove nested commits from the batch execution path.
- [ ] Roll back facts, entities, links, sources, relationships, and tombstones
      together.
- [ ] Rebuild affected banks only after successful commit.
- [ ] Keep batch ingestion non-model-callable initially.
- [ ] Migrate built-in import only after compatibility tests pass.

### Batch tests

- [ ] Successful atomic batch commits all associated rows.
- [ ] Mid-batch failure rolls back every mutation.
- [ ] Dry-run performs zero writes.
- [ ] Every per-item status is covered.
- [ ] Provenance is complete.
- [ ] Same text in different namespaces is allowed.
- [ ] Same text in one namespace consolidates.
- [ ] Tombstone/reintroduction policy is preserved.
- [ ] Excessive batch/item input is rejected safely.
- [ ] Concurrent batches do not corrupt SQLite.
- [ ] Bank rebuild count is bounded.
- [ ] Built-in-memory import retains prior behavior.

## 8. Phase 5 — Scoped relationships

- [ ] Decide additive-column versus new-table migration after schema fixture
      testing.
- [ ] Add namespace to relationship ownership.
- [ ] Add a composite namespace/source/target/type unique index.
- [ ] Add extraction method, confidence, and timestamps.
- [ ] Optionally link relationships to source facts.
- [ ] Preserve legacy relationships as unscoped only.
- [ ] Create a focused `relationships.py` service.
- [ ] Implement add/upsert/remove/list operations.
- [ ] Implement bounded breadth-first traversal.
- [ ] Add depth, node, edge, and deadline limits.
- [ ] Invalidate/remove derived edges after forget.
- [ ] Recompute/invalidate stale edges after supersession.
- [ ] Apply existing shared-memory allowlists to shared traversal.
- [ ] Keep relationship mutations out of model tools initially.

### Relationship tests

- [ ] Relationship uniqueness is namespace-scoped.
- [ ] Identical entity names in two chats do not leak edges.
- [ ] Direction and relationship type are preserved.
- [ ] Upsert does not duplicate edges.
- [ ] Strength/confidence are clamped.
- [ ] Traversal respects depth/node/edge/deadline limits.
- [ ] Cycles terminate safely.
- [ ] Forget invalidates derived edges.
- [ ] Superseded facts do not contribute stale active edges.
- [ ] Shared traversal follows enablement and category allowlists.
- [ ] Migration does not broadcast legacy edges into chat namespaces.
- [ ] Health diagnostics detect orphan edges without repairing them.

## 9. Phase 6 — Maintenance commands

- [ ] Add `hermes memory rebuild-vectors`.
- [ ] Add `hermes memory rebuild-banks`.
- [ ] Add `hermes memory backfill-relationships --dry-run`.
- [ ] Add explicit `--apply` behavior.
- [ ] Add `hermes memory verify` or reuse health consistently.
- [ ] Resolve default/named profiles safely.
- [ ] Acquire a maintenance lock for writes.
- [ ] Create and verify a backup before apply operations.
- [ ] Display exact target before confirmation.
- [ ] Support interruption and idempotent restart.
- [ ] Avoid printing fact content or raw identities.
- [ ] Define stable JSON output and exit codes.
- [ ] Keep commands unavailable through gateways/model tools.

### Maintenance tests

- [ ] Dry-run changes no files or rows.
- [ ] Apply refuses to run without required confirmation/flag.
- [ ] Apply aborts when backup verification fails.
- [ ] Lock prevents unsafe concurrent maintenance.
- [ ] Interrupted operation can restart safely.
- [ ] Repeated rebuild is idempotent.
- [ ] Named profile operation cannot touch another profile.
- [ ] Output contains no secrets or raw chat identity.

## 10. Database migrations

- [ ] Add internal schema version metadata.
- [ ] Detect pre-PR2 schema.
- [ ] Detect pre-PR3 global-unique schema.
- [ ] Detect current schema.
- [ ] Make new migrations idempotent.
- [ ] Preserve FTS triggers and foreign keys during rebuilds.
- [ ] Validate FK/FTS parity before commit.
- [ ] Leave original DB usable on migration failure.
- [ ] Do not silently delete legacy banks or relationships.
- [ ] Document downgrade behavior.

### Migration tests

- [ ] Fresh DB creates latest schema.
- [ ] Pre-PR2 fixture migrates.
- [ ] Pre-PR3 fixture migrates.
- [ ] Current DB migration is a no-op/idempotent.
- [ ] Facts and categories survive.
- [ ] Active/superseded state and revision links survive.
- [ ] Provenance survives.
- [ ] FTS search works after migration.
- [ ] Tombstones survive.
- [ ] Relationship scope migration is correct.
- [ ] Injected migration failure preserves the original DB.
- [ ] Reopen after successful migration remains healthy.

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
