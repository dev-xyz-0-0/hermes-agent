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

- [x] Keep viewer dependencies optional. (`plugins/memory/holographic/viewer.py`
      uses only the Python stdlib — `http.server`, `sqlite3`, `html`,
      `urllib.parse` — no new third-party dependency. `fastapi`/`uvicorn`
      are declared in `pyproject.toml` but unused by any existing runtime
      code path in this repo, so building on them would create a new
      dependency *relationship* even though the packages were already
      listed; stdlib avoids that entirely and is trivially provably
      optional — see `tests/plugins/memory/test_holographic_viewer_optional_import.py`.)
- [x] Bind to loopback by default. (`create_server(..., host="127.0.0.1")`
      default; `_validate_host` only accepts `127.0.0.1`/`localhost`/`::1`.)
- [x] Disable viewer by default. (Nothing starts it except the operator
      explicitly running `hermes memory viewer`; see
      `hermes_cli/memory_setup.py`'s `cmd_viewer`.)
- [x] Make initial viewer read-only. (Every connection opened with SQLite's
      `mode=ro` URI flag — identical technique to
      `diagnostics.run_diagnostics` — plus every mutation HTTP method is
      explicitly rejected with 405; no route ever calls a `MemoryStore`
      write method or constructs a writable `MemoryStore` at all.)
- [x] Require explicit profile selection. (Resolved through the same
      `_holographic_db_path()`/`get_hermes_home()` helpers every other
      `hermes memory` subcommand uses; there is no "all profiles" mode.)
- [x] Require explicit namespace selection. (`/namespace`, `/fact`, and
      `/entity` all require an explicit `ns=<digest>` query parameter;
      missing it is a 400, not a fallback to "no filter.")
- [x] Avoid an all-namespaces default view. (`/` is a namespace *selector*
      — opaque digests + counts only; it never renders a fact.)
- [x] Redact secret-shaped fact content. (New
      `plugins/memory/holographic/__init__.py:redact_secret_shapes()`
      reuses the exact same `_RE_SECRET_SHAPE`/`_RE_PRIVATE_KEY`/
      `_RE_RECOVERY_PHRASE`/`_RE_CREDENTIAL` detectors the capture-time
      filter already uses, applied to every rendered fact's content/tags
      and every rendered entity name as defense-in-depth for data that
      predates or bypasses that filter.)
- [x] Minimize displayed provenance. (Fact pages show status, category,
      trust_score, confidence, source_type, extraction_method,
      created_at/updated_at — never `fact_sources.chat_id`/`platform`/
      `author`, the on-disk DB path, or any environment value.)
- [x] Distinguish active/superseded/forgotten states. (Distinct CSS status
      badges for active vs. superseded in every listing; forgotten facts
      are hard-deleted by `remove_fact` so there is no content left to
      show — represented honestly as an opaque tombstone count in a
      visually distinct "forgotten" badge, never a fabricated entry.)
- [x] Show revision and relationship provenance safely. (Fact pages link
      supersession ancestors/descendant within the same namespace, bounded
      to `_SUPERSESSION_CHAIN_MAX_HOPS`; entity pages list namespace-scoped
      relationships and a bounded BFS traversal via
      `relationships.list_relationships`/`relationships.traverse`, called
      through `_ReadOnlyStoreView` — see below.)
- [x] Enforce pagination and query limits. (Every list view — namespace
      selector, fact list, relationship list, traversal — is capped at
      `MAX_PAGE_SIZE` (50) rows regardless of client-requested
      `per_page`; search (`q`) is always a bound SQL parameter, never
      interpolated.)
- [x] Expose no raw SQL, restore, file access, or mutation endpoint. (Only
      four fixed, parameterized read queries exist; every non-GET method
      405s; no route accepts a file path, SQL fragment, or restore action.)
- [x] Require authentication and CSRF protection before any future write or
      non-loopback mode. (Satisfied by NOT offering either: non-loopback
      binding raises `NonLoopbackBindingNotSupported` before any socket is
      opened — a deliberate "not supported at all" choice, documented in
      the module docstring, rather than a flag that could be mistaken for
      a real safeguard with no auth/CSRF behind it. No write endpoint
      exists at all in this phase.)

Deliberate design note — `--show-raw-identity`: implemented as an explicit,
server-start-time-only CLI flag (never a per-request query parameter). It is
off by default; when passed, it only adds the raw namespace string next to
its digest on the `/` selector page — it never changes which routes exist,
never disables redaction, and never affects the read-only/loopback-only
guarantees. This matches the plan's own allowance ("opaque labels unless the
operator explicitly requests identity metadata") as a narrow, documented,
opt-in escape hatch rather than a default behavior.

A `_ReadOnlyStoreView` adapter class (in `viewer.py`) implements only the two
read methods `relationships.list_relationships`/`relationships.traverse`
need (`list_relationships_for_entity`, `relationship_neighbors`), backed
directly by the module's own `mode=ro` connection — a real `store.MemoryStore`
is deliberately never constructed in this module, because its constructor
opens a read-write connection and can run schema migrations, which would
break the read-only guarantee.

### Viewer tests

All in `tests/plugins/memory/test_holographic_viewer.py` (HTTP-level, 44
tests), `tests/plugins/memory/test_holographic_viewer_optional_import.py`
(fresh-interpreter subprocess import checks, 7 tests), and
`tests/hermes_cli/test_memory_viewer_cli.py` (CLI wiring, 6 tests) — 57 new
tests total.

- [x] Default bind address is loopback. (Tests construct the real
      `ViewerServer`/socket via `create_server` and assert
      `server.server_address[0] == "127.0.0.1"`, plus confirm
      `0.0.0.0`/other hosts raise before any socket opens.)
- [x] Mutation HTTP methods/endpoints are absent or rejected. (Every
      registered route — `/`, `/namespace`, `/fact`, `/entity`, and an
      unknown path — is exercised with POST/PUT/DELETE/PATCH over a real
      HTTP connection and asserted to return 404/405, with `Allow: GET` on
      405s.)
- [x] Namespace selection is explicit. (`/namespace`/`/fact` without `ns`
      400; index never renders a fact.)
- [x] Cross-namespace access is rejected. (Two real namespace fixtures;
      namespace B's fact_id requested under namespace A's digest 404s;
      namespace B's entity_id requested under namespace A's digest 404s; a
      raw namespace string passed directly as `ns` also 404s rather than
      being accepted as a selector.)
- [x] Pagination/query limits are enforced. (A corpus of `MAX_PAGE_SIZE + 25`
      facts and `MAX_PAGE_SIZE + 10` namespaces is seeded; default page,
      an oversized `per_page`, and a past-the-end `page` are all asserted
      to return at most `MAX_PAGE_SIZE` rows, never the full set.)
- [x] Secrets and raw identities are absent from rendered output. (A fact
      seeded with a real `sk-ant-...`-shaped secret and a fact tagged with
      a real `telegram:chat_...` namespace string are asserted absent from
      every rendered page, with `[REDACTED:...]`/the opaque digest shown
      instead; a separate test confirms `--show-raw-identity` is the only
      way the raw string appears.)
- [x] Normal Hermes startup does not import optional viewer dependencies.
      (Fresh-interpreter subprocess checks of `run_agent`, `cli`,
      `hermes_cli.main`, `model_tools`, `plugins.memory.holographic`, and
      `hermes_cli.memory_setup` all assert
      `"plugins.memory.holographic.viewer" not in sys.modules`; a sanity
      check confirms the marker mechanism itself works by explicitly
      importing the viewer module and seeing it appear.)

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
