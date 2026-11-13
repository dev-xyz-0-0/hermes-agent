# Holographic Demo Reference Integration — Implementation Plan

Date: 2026-11-12

Status: Proposed

Primary target: `plugins/memory/holographic/`

Reference implementation: `demo-reference/holographic-memory-main/`

Companion checklist: `docs/plans/demo-reference-plan/checklist.md`

## 1. Objective

Integrate the useful, still-missing capabilities from the Holographic Memory
demo into Hermes' production Holographic plugin without weakening the safety,
isolation, lifecycle, or compatibility guarantees already delivered by the
Holographic PR1–PR3 work.

The integration should add:

1. reproducible retrieval benchmarks and read-only health diagnostics;
2. namespace-specific HRR memory banks;
3. deterministic fuzzy-duplicate evidence in consolidation;
4. safe transactional batch ingestion;
5. namespace-scoped entity relationships and bounded relationship traversal;
6. profile-aware maintenance and backfill commands;
7. an optional local, read-only diagnostic viewer; and
8. retrieval telemetry suitable for evaluating future weight learning.

This is a selective integration, not a wholesale copy. The production plugin
remains authoritative where it differs from the demo.

## 2. Existing System Baseline

The current production plugin already goes substantially beyond the demo and
must not regress:

- SQLite WAL storage with locking and profile-aware paths.
- Per-chat namespace isolation for multi-tenant gateways.
- Fail-closed behavior when a multi-tenant chat identity is missing.
- Automatic recall with relevance, repeat, token, and timeout limits.
- Rule/hybrid capture with input, secret, and instruction-shape filtering.
- Consolidation into insert, ignore, supersede, forget, or no-op outcomes.
- Fact status, revision history, provenance, and source metadata.
- Forgotten-fact tombstones that survive backup and restore.
- Backup creation, verification, restore safeguards, and retention.
- Shared-memory controls with explicit sharing and category allowlists.
- Optional NumPy degradation to FTS-only retrieval.
- Existing `fact_store` and `fact_feedback` tool contracts.

The demo's `holographic.py` HRR implementation is byte-for-byte identical to
the production copy. It should not be replaced or rewritten as part of this
project.

## 3. Design Principles

1. **Preserve isolation.** Every new read and write must accept or derive a
   namespace. Multi-tenant operations must never fall back to the unscoped
   namespace when identity resolution fails.
2. **Use existing provider boundaries.** Chat-facing operations go through
   `HolographicMemoryProvider`; operator maintenance goes through explicit CLI
   commands or internal service functions.
3. **Keep prompt caching stable.** Do not reload provider configuration,
   change tool schemas, or rebuild the system prompt during a conversation.
4. **Prefer additive, reversible migrations.** Back up before destructive
   maintenance and make migrations idempotent.
5. **Do not revive forgotten data.** Batch import, relationship backfill, and
   restore must honor tombstones.
6. **Do not duplicate security logic.** Reuse production write validation,
   capture normalization, consolidation, provenance, and namespace checks.
7. **Bound expensive work.** Benchmarking, graph traversal, backfill, and
   viewer queries need explicit limits and must not run on the response path.
8. **Measure before tuning.** Add telemetry and a stable benchmark corpus
   before implementing adaptive retrieval weights.
9. **Remain useful without NumPy.** New HRR features must degrade cleanly to
   existing FTS/Jaccard behavior.
10. **Keep operator tools local and explicit.** No model-callable restore,
    arbitrary namespace selection, or unrestricted database query surface.

## 4. Scope and Non-Goals

### In scope

- Port and adapt demo benchmark/health concepts.
- Improve current plugin internals where the demo exposes a genuine gap.
- Add safe operator-facing maintenance entry points.
- Add schema migrations required for scoped relationships and telemetry.
- Add unit, migration, integration, concurrency, security, and compatibility
  tests.
- Update the Holographic README and relevant CLI documentation.

### Out of scope

- Replacing production capture with the demo's simpler session-end extractor.
- Replacing production security filters with demo regexes.
- Making memory global or disabling fail-closed isolation.
- Hard-coding chat IDs or accepting arbitrary chat namespaces from the model.
- Exposing restore, raw SQL, or relationship backfill as model tools.
- Copying the demo MCP server without a separate authentication design.
- Enabling automatic learned retrieval weights in the first rollout.
- Changing past conversation context or toolsets mid-session.
- Editing a user's existing SQLite data outside tested migrations.

## 5. Target Architecture

Keep the existing module boundaries and add focused modules rather than
growing `plugins/memory/holographic/__init__.py` further:

```text
plugins/memory/holographic/
├── __init__.py          provider lifecycle and stable provider API
├── holographic.py       unchanged HRR primitives
├── store.py             storage, migrations, batch transaction primitives
├── retrieval.py         ranking and namespace-aware retrieval
├── capture.py           candidate extraction and safety filtering
├── consolidation.py     duplicate/correction/forget decisions
├── backup.py            existing backup/verify/restore behavior
├── relationships.py     new scoped relationship service and traversal
├── diagnostics.py       new read-only health checks
├── benchmark.py         new benchmark runner/library
├── maintenance.py       new operator maintenance service
└── telemetry.py         optional bounded retrieval event aggregation
```

CLI wiring should live in `hermes_cli/memory_setup.py` or a dedicated
`hermes_cli/memory_commands.py`, depending on the current `hermes memory`
subcommand structure. Reuse the existing top-level `hermes memory` command;
do not introduce chat slash commands for administrative operations.

## 6. Delivery Strategy

Deliver this work as independently reviewable phases. Each phase must leave
the repository releasable and must pass its targeted tests plus the existing
Holographic suite. Later phases may be deferred without invalidating earlier
ones.

### Phase 0 — Baseline, fixtures, and compatibility contract

Before implementation:

1. Run and record the existing Holographic tests.
2. Record the current database schema, indexes, triggers, and tool schema.
3. Add reusable fixtures for:
   - unscoped CLI memory;
   - two isolated gateway namespaces;
   - a threaded chat namespace;
   - shared-memory namespace;
   - NumPy available/unavailable modes;
   - a pre-PR2 database;
   - a pre-PR3 database with global `UNIQUE(content)`;
   - a current database containing active, superseded, and forgotten facts.
4. Snapshot the public `fact_store` action enum and provider configuration
   defaults so accidental contract changes fail tests.
5. Add a small deterministic benchmark gold set under
   `tests/fixtures/holographic/`; do not make tests depend on the demo folder.

Compatibility gate:

- Existing databases open without data loss.
- Existing provider configuration remains valid.
- Existing tool calls retain their current response shapes.
- CLI continues to use the unscoped namespace.
- Gateways remain scoped and fail closed when identity is absent.

### Phase 1 — Benchmark and read-only health diagnostics

Port the ideas from the demo benchmark and health scripts into importable,
testable production modules. Do not import code at runtime from
`demo-reference/`.

#### Benchmark library

Add a benchmark runner that accepts an explicit temporary or operator-selected
database and gold-set path. Metrics should include:

- Recall@1, Recall@5, and mean reciprocal rank.
- Empty-result rate and query error rate.
- Per-strategy contribution: FTS, Jaccard, and HRR.
- Results with and without temporal decay.
- Active/superseded filtering correctness.
- Namespace leakage count, which must always be zero.
- P50/P95 query latency.
- Result stability across repeated deterministic runs.

The runner must not write feedback, increment production counters, or alter
trust scores. Add a retrieval option or read-only evaluation path that avoids
normal retrieval side effects.

#### Health diagnostics

Add read-only checks for:

- `PRAGMA integrity_check` and `foreign_key_check`.
- Expected tables, columns, indexes, and FTS triggers.
- FTS row parity with facts.
- Orphaned `fact_entities`, `fact_sources`, and relationship rows.
- Invalid `superseded_by` references and revision cycles.
- Missing, malformed, or wrong-dimension HRR blobs.
- Memory-bank fact counts and dimensions.
- Facts that violate status or trust-score constraints.
- Namespace counts without exposing raw chat identifiers in standard output.
- Backup manifest/checksum validity when a backup path is supplied.

Health commands are read-only by default. Any later repair operation must be
an explicit separate command with dry-run and backup requirements.

#### CLI

Extend the existing `hermes memory` command family with:

```text
hermes memory health [--profile NAME] [--json]
hermes memory benchmark --gold-set PATH [--profile NAME] [--json]
```

The CLI resolves profile paths through existing profile/HERMES_HOME helpers.
It must not accept a raw chat namespace unless an authenticated administrative
namespace-selection design is added later.

### Phase 2 — Namespace-specific HRR banks

Current category banks use `cat:<category>` and aggregate all namespaces.
Production retrieval therefore skips the bank optimization for scoped calls.
Replace this with namespace-specific banks while preserving backward
compatibility for unscoped CLI facts.

#### Bank identity

- Use a deterministic opaque key derived from `(namespace, category)`.
- Do not store a raw chat ID in `bank_name` when an opaque digest will work.
- Preserve a stable key for `namespace == ""` so existing CLI behavior remains
  available.
- Include only active facts in rebuilt banks.

Suggested internal API:

```python
bank_name(namespace: str, category: str) -> str
rebuild_bank(category: str, namespace: str) -> None
rebuild_all_vectors(dim: int | None = None, namespace: str | None = None) -> int
```

Update every fact mutation path to rebuild only affected namespace/category
pairs. Batch operations should collect affected pairs and rebuild once after
commit.

#### Migration

No destructive table migration should be necessary because `memory_banks`
already keys on text. On first compatible startup or explicit rebuild:

1. ignore legacy cross-namespace category banks for scoped retrieval;
2. lazily create scoped banks on mutation, or eagerly create them via the
   maintenance command;
3. remove legacy banks only during an explicit cleanup/rebuild after backup.

Never temporarily use a legacy global bank to serve a scoped query.

### Phase 3 — Deterministic fuzzy evidence in consolidation

Port the demo's character N-gram Jaccard concept, but use it as evidence in
`consolidation.py`, not as an automatic merge inside `MemoryStore.add_fact()`.

#### Behavior

- Normalize text consistently with existing capture rules.
- Compute token overlap and character N-gram overlap.
- Consider only active facts in the same namespace and compatible scope.
- Exact normalized matches remain duplicates.
- High fuzzy similarity may classify a candidate as a probable duplicate.
- A conflicting value must not be merged merely because surrounding words
  overlap.
- Corrections still require an explicit, unambiguous target.
- Ambiguous matches remain no-ops or independent facts according to existing
  consolidation policy.
- Security rejection happens before fuzzy comparison.

Thresholds must be named constants, documented, and calibrated against a
checked-in evaluation corpus. Do not tune thresholds solely to unit tests.

Add diagnostics to `ConsolidationResult` for the selected strategy and score,
but preserve existing fields and actions for compatibility.

### Phase 4 — Safe transactional batch ingestion

Add a production batch API inspired by the demo's `add_facts_batch()`, but do
not bypass capture or consolidation.

Suggested service contract:

```python
ingest_candidates_batch(
    candidates: Sequence[FactCandidate],
    *,
    namespace: str,
    source: SourceContext,
    atomic: bool = True,
    dry_run: bool = False,
) -> BatchResult
```

`BatchResult` should provide per-item status and aggregate counts:

- `inserted`
- `duplicate`
- `superseded`
- `forgotten`
- `rejected`
- `noop`
- `failed`

Requirements:

1. Require the caller to supply an already resolved namespace. Provider paths
   must reject calls while isolation is blocked.
2. Run every item through existing content validation, scope normalization,
   consolidation, and tombstone policy.
3. Record provenance for every successful mutation.
4. Use one transaction in atomic mode and roll back facts, entities, links,
   sources, relationships, and tombstones together on failure.
5. Avoid nested commits in current store helpers by introducing transaction-
   aware private primitives or a store transaction context manager.
6. Rebuild affected HRR banks after a successful commit, once per affected
   namespace/category pair.
7. Provide bounded batch size and content-length limits.
8. Keep this an internal/operator API initially; do not add a model-callable
   bulk-ingest action in the first release.

Use the batch service for built-in memory import and future backfill paths only
after compatibility tests prove response and provenance behavior.

### Phase 5 — Namespace-scoped entity relationships

The demo exposes relationship insertion and graph concepts; production retains
an `entity_relationships` table but lacks a complete namespace-safe service.

#### Schema

Add or rebuild relationship storage so each relationship is scoped. Prefer a
new table or an additive `namespace` column with an explicit composite unique
index:

```text
(namespace, source_entity_id, target_entity_id, relationship_type)
```

If entity rows themselves remain shared, relationship queries must always join
through scoped facts or apply the relationship namespace explicitly. A chat
must never infer another chat's relationship merely because entity names map
to the same global entity IDs.

Store:

- relationship type;
- bounded strength/confidence;
- namespace;
- extraction method;
- created/updated timestamps; and
- optional source fact IDs where both source facts belong to the namespace.

#### Service API

Create `relationships.py` with methods for:

- add/upsert relationship;
- remove relationship;
- list relationships for an entity;
- bounded breadth-first traversal;
- remove or invalidate relationships derived only from forgotten facts;
- mark or recompute relationships affected by supersession.

Traversal must have maximum depth, node, edge, and time limits. Returned data
must contain only the caller's namespace plus explicitly enabled shared-memory
data under existing shared-memory rules.

#### Extraction and backfill

- Do not infer relationships on the synchronous response path initially.
- Begin with explicit deterministic patterns and/or operator-triggered
  backfill.
- If LLM extraction is later used, require structured output validation,
  existing content safety checks, source attribution, and confidence limits.
- Backfill defaults to dry-run and writes only after a verified backup.

Do not expose relationship mutation directly to the model in the first
release. A later read-only tool action may be considered after the isolation
tests are mature.

### Phase 6 — Maintenance commands

Add explicit operator commands backed by `maintenance.py`:

```text
hermes memory rebuild-vectors [--profile NAME] [--dry-run]
hermes memory rebuild-banks [--profile NAME] [--dry-run]
hermes memory backfill-relationships [--profile NAME] --dry-run
hermes memory backfill-relationships [--profile NAME] --apply
hermes memory verify [--profile NAME] [--json]
```

Rules:

- Resolve all paths with `get_hermes_home()`/profile helpers.
- Never hardcode `~/.hermes`.
- Acquire a maintenance lock before writes.
- Refuse write maintenance while the target DB has an active incompatible
  writer, or document and enforce the safe locking protocol.
- Create and verify a backup before `--apply` maintenance.
- Print the exact target profile/path before confirmation.
- Support interruption and idempotent restart.
- Report counts without printing secret fact content.
- Return stable exit codes and JSON output for automation.
- Never expose these operations through gateway slash commands or model tools.

If the existing CLI architecture requires a new `CommandDef`, add it only for
the top-level user-visible command and derive help/autocomplete from
`COMMAND_REGISTRY`. Prefer extending the existing `hermes memory` argparse
subcommands when available.

### Phase 7 — Optional local diagnostic viewer

Adapt the demo viewer only after Phases 1–6 are stable.

Security and compatibility requirements:

- Disabled by default and bound to loopback by default.
- Read-only by default; no restore, raw SQL, arbitrary file access, or hidden
  mutation endpoints.
- Profile selected explicitly by the operator.
- Namespace views use opaque labels unless the operator explicitly requests
  identity metadata.
- No automatic “all namespaces” landing page.
- Fact content passes display redaction; provenance fields are minimized.
- Active, superseded, and forgotten records are visually distinct.
- Revision chains and relationship provenance are inspectable.
- Pagination and query limits prevent loading the full database.
- CSRF protection and authentication are required before any future write
  operation or non-loopback binding.
- Viewer dependencies remain optional and do not affect normal CLI/gateway
  startup.

### Phase 8 — Retrieval telemetry and future weight learning

Do not copy the demo's `learn_weights_from_feedback()` heuristic directly. It
cannot attribute success to a retrieval strategy and can amplify popularity
feedback loops.

Instead, add optional bounded telemetry capturing, without fact content:

- query/event identifier;
- provider/profile-local namespace digest;
- result fact ID or opaque digest;
- FTS, Jaccard, and HRR component scores;
- temporal and trust multipliers;
- final rank;
- explicit helpful/unhelpful feedback; and
- timestamp and algorithm version.

Telemetry must be off by default or have bounded local retention, remain
profile-scoped, and never contain prompts, fact text, credentials, raw chat
IDs, or user names.

After collecting representative benchmark and opt-in real-world data, a
separate design may evaluate offline weight recommendations. Runtime learned
weights must not change during an active conversation because doing so could
alter behavior and prompt-cache assumptions unpredictably.

## 7. Database Migration Plan

Use an explicit internal schema version rather than relying only on missing
column checks for new work. A small metadata table is sufficient:

```sql
CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Migration rules:

1. Detect and preserve pre-PR2, pre-PR3, and current schemas.
2. Run migrations inside a transaction where SQLite permits it.
3. Recreate FTS triggers and foreign keys correctly when table rebuilds are
   required.
4. Run `foreign_key_check` and targeted parity checks before commit.
5. Leave the original database untouched if migration validation fails.
6. Do not silently delete legacy banks or relationships.
7. Make every migration idempotent and test reopening after partial failure.
8. Document downgrade limitations. New code should tolerate unused additive
   tables where possible.

The relationship migration needs special care because shared entity IDs do
not themselves establish relationship ownership. Existing unscoped
relationships should remain unscoped; they must not be copied into every chat
namespace.

## 8. Configuration Plan

Add only settings needed for runtime behavior. Suggested keys under the
existing Holographic plugin configuration:

| Key | Default | Purpose |
|---|---:|---|
| `fuzzy_dedup_enabled` | `true` | Enable deterministic fuzzy evidence |
| `fuzzy_dedup_threshold` | calibrated constant | Optional bounded override |
| `scoped_hrr_banks` | `true` | Use per-namespace/category HRR banks |
| `relationship_recall_enabled` | `false` | Gate relationship-assisted recall |
| `relationship_max_depth` | `2` | Bound traversal |
| `relationship_max_nodes` | `50` | Bound traversal |
| `retrieval_telemetry` | `false` | Enable content-free local telemetry |
| `retrieval_telemetry_retention` | bounded | Maximum retained events |

Validate and clamp numeric values in one place. Unknown or invalid values
should log a safe warning and fall back to documented defaults.

Do not add configuration for bypassing namespace isolation. Existing explicit
isolation controls retain their current behavior and defaults.

## 9. Detailed Test Plan

All tests must use temporary `HERMES_HOME`/databases. No test may read or write
the developer's `~/.hermes` directory or production memory.

### 9.1 Baseline regression tests

- Existing add/search/list/update/remove/history/share/feedback actions retain
  response shapes and semantics.
- Automatic recall remains relevance-gated, budgeted, repeat-suppressed, and
  fail-open on internal retrieval errors.
- Automatic capture remains bounded and idempotent.
- Superseded facts do not appear in ordinary recall.
- Forgotten facts remain absent after backup restore.
- Provider shutdown remains idempotent and closes executors/connections.
- NumPy-unavailable mode retains FTS retrieval and emits the existing warning.

### 9.2 Isolation tests

- Two chats may store identical text as independent facts.
- Search, probe, related, reason, contradiction, graph traversal, batch import,
  diagnostics, and bank lookup never return the other chat's data.
- Thread IDs produce separate namespaces where currently supported.
- Shared facts are visible only under existing shared-memory settings and
  category allowlists.
- Missing chat identity on a multi-tenant platform blocks every new read and
  write path.
- CLI remains unscoped and backward-compatible.
- Named Hermes profiles use different database files and maintenance targets.

### 9.3 HRR bank tests

- Bank keys differ across namespaces for the same category.
- Raw chat IDs do not appear in bank names.
- Banks contain only active facts.
- Insert/update/supersede/forget changes only affected banks.
- Batch ingestion rebuilds each affected bank once.
- Scoped probe uses only the scoped bank.
- Legacy global banks are never used for scoped retrieval.
- Rebuild produces stable fact counts and dimensions.
- Dimension changes rebuild vectors and banks consistently.
- No-NumPy operation skips banks without breaking storage or search.

### 9.4 Fuzzy consolidation tests

- Paraphrases of the same durable fact become duplicates.
- Exact matches remain duplicates with unchanged behavior.
- Similar scaffolding with different critical values remains separate.
- Explicit correction supersedes exactly one unambiguous target.
- Tied or low-confidence correction does not supersede anything.
- Project/user scopes do not cross-match.
- Namespace boundaries are respected.
- Unicode, punctuation, and long-input normalization are bounded.
- Rejected secret/instruction-shaped candidates never reach similarity logic.
- Threshold boundary cases are deterministic.

### 9.5 Batch ingestion tests

- Successful atomic batch commits facts, entity links, sources, and banks.
- A failure in the middle rolls back every mutation in atomic mode.
- Dry-run returns planned outcomes without writes or counter changes.
- Per-item statuses are correct for duplicates, corrections, forgets,
  rejection, and no-op.
- Provenance is recorded for every successful item.
- Duplicate content in different namespaces is allowed; within one namespace
  it consolidates safely.
- Tombstoned content follows the documented reintroduction policy.
- Batch size/content limits reject excessive input safely.
- Concurrent batches do not corrupt SQLite or leak transactions.
- Built-in-memory import remains compatible when switched to the batch API.

### 9.6 Relationship tests

- Relationship uniqueness is scoped by namespace.
- Same entity names in two chats do not expose cross-chat edges.
- Direction and type are preserved.
- Upsert updates strength/confidence without duplicating edges.
- Traversal respects depth, node, edge, and deadline limits.
- Cycles do not loop indefinitely.
- Forgotten source facts remove or invalidate derived relationships.
- Superseded facts do not contribute stale active relationships.
- Shared-memory traversal obeys allowlists and explicit enablement.
- Migration preserves legacy unscoped relationships without broadcasting them.
- Orphan detection reports but does not mutate in health mode.

### 9.7 Diagnostics and benchmark tests

- Healthy temporary DB returns success and stable JSON schema.
- Corrupt/missing index, trigger, vector, bank count, FK, and FTS parity
  fixtures yield the expected diagnostic codes.
- Diagnostics never mutate the DB; compare file hash/change counters where
  practical.
- Standard output does not disclose raw namespace/chat identity or secrets.
- Benchmark metrics match a small deterministic corpus.
- Benchmark evaluation does not increment retrieval or feedback counters.
- Namespace leakage metric detects a deliberately broken fixture.
- NumPy/no-NumPy benchmark modes report capabilities accurately.
- JSON output is machine-readable and exit codes are stable.

### 9.8 Migration and backup tests

- Fresh database creates the latest schema.
- Pre-PR2 database migrates successfully.
- Pre-PR3 globally unique database migrates successfully.
- Current database migration is idempotent.
- Active, superseded, provenance, FTS, and tombstone data survive migration.
- Failed migration leaves the original database usable.
- Backup is created and verified before maintenance apply operations.
- Restore cannot reactivate forgotten facts.
- Restore/reopen rebuild or invalidate derived banks safely.

### 9.9 CLI tests

- Help and autocomplete expose intended `hermes memory` subcommands.
- Commands resolve default and named profile paths correctly.
- `--json` emits no decorative text.
- Dry-run performs no writes.
- Apply mode requires the intended confirmation/flag and verified backup.
- Non-interactive mode has deterministic exit codes.
- Gateway slash-command and Telegram/Slack command menus do not expose
  maintenance commands.
- CLI commands never hardcode `~/.hermes`.

### 9.10 Viewer tests, if Phase 7 is implemented

- Server binds to loopback by default.
- Mutation methods/endpoints are absent or rejected.
- Namespace selection is explicit and access-controlled.
- Pagination and query limits are enforced.
- Secret-shaped content is redacted.
- No raw database path, environment value, chat ID, or credential leaks.
- Optional viewer dependencies are not imported during normal Hermes startup.

### 9.11 Performance and concurrency tests

- Compare benchmark latency before/after scoped banks on a fixed corpus.
- Large but bounded batches complete without per-fact bank rebuilds.
- Concurrent recall and capture do not produce transaction errors.
- Maintenance locking prevents unsafe concurrent writes.
- Traversal and diagnostics honor deadlines.
- Memory usage remains bounded for benchmark, telemetry, and viewer queries.

## 10. Validation Sequence

For every phase:

1. Activate the repository environment: `source venv/bin/activate`.
2. Run the new phase-specific tests.
3. Run all Holographic tests:
   `python -m pytest tests/plugins/memory/ -q`.
4. Run memory-provider lifecycle and gateway boundary tests.
5. Run CLI command/configuration tests for phases touching CLI behavior.
6. Run gateway tests for phases touching identity or namespaces.
7. Run `python -m pytest tests/ -q` before merging, per repository policy.
8. Run `git diff --check` and inspect the dirty worktree for unrelated changes.
9. Record exact pass/fail/skip/warning counts in the PR description.

Suggested targeted command set:

```bash
source venv/bin/activate
python -m pytest tests/plugins/memory/ -q
python -m pytest tests/agent/test_memory_provider.py -q
python -m pytest tests/run_agent/test_memory_provider_release.py -q
python -m pytest tests/run_agent/test_memory_provider_shutdown.py -q
python -m pytest tests/gateway/test_session_boundary_hooks.py -q
python -m pytest tests/hermes_cli/ -q
python -m pytest tests/gateway/ -q
python -m pytest tests/ -q
```

## 11. Documentation Deliverables

Update `plugins/memory/holographic/README.md` with:

- scoped-bank behavior and migration notes;
- fuzzy-consolidation semantics and limitations;
- batch ingestion behavior and size limits;
- relationship ownership and traversal limits;
- health, benchmark, rebuild, and backfill commands;
- profile targeting and backup requirements;
- NumPy degradation behavior;
- viewer security model, if implemented;
- telemetry fields, retention, and privacy, if implemented; and
- rollback instructions for every runtime-configurable feature.

Keep a migration note for users upgrading from older plugin schemas. Document
that `demo-reference/` is design/reference material and is not imported at
runtime.

## 12. Rollout and Rollback

### Rollout

1. Merge diagnostics/benchmark tooling first because it is read-only.
2. Enable scoped banks with compatibility fallback only for unscoped CLI
   memory; scoped gateways must never use global banks.
3. Roll out fuzzy consolidation behind a configuration flag and compare
   evaluation results before making it unconditional.
4. Keep batch ingestion internal until migration/import tests are stable.
5. Keep relationship-assisted recall off by default through at least one
   release while backfill quality is evaluated.
6. Keep viewer and telemetry optional.

### Rollback

- Disable fuzzy consolidation to return to existing token-overlap behavior.
- Disable relationship-assisted recall without deleting relationship data.
- Stop using scoped banks and fall back to per-fact scoped scoring; never fall
  back to cross-namespace banks.
- Derived banks and telemetry may be deleted/rebuilt after backup because facts
  remain authoritative.
- Relationship tables should remain additive on downgrade; older code ignores
  them.
- Do not roll back by disabling profile isolation or deleting tombstones.

## 13. Acceptance Criteria

The project is complete when:

1. Benchmark and health commands operate on temporary/default/named profiles
   without mutating memory or exposing identities.
2. Scoped HRR banks improve or preserve retrieval performance while producing
   zero cross-namespace results.
3. Fuzzy consolidation improves paraphrase deduplication without merging the
   conflicting-value and scope-isolation evaluation cases.
4. Batch ingestion is atomic, provenance-complete, tombstone-aware, bounded,
   and namespace-safe.
5. Relationship storage and traversal are namespace-scoped, bounded, and
   invalidated correctly after forget/supersede operations.
6. All migrations are idempotent and preserve facts, history, sources, FTS,
   tombstones, and isolation.
7. Existing `fact_store`, automatic recall/capture, backup/restore, CLI, and
   gateway behavior remains compatible.
8. Missing multi-tenant identity still fails closed across every added path.
9. The full test suite passes, and exact results are recorded.
10. Documentation, migration notes, configuration defaults, operational
    commands, rollback steps, and security limitations are complete.

## 14. Deferred Follow-ups

These require separate designs after the above acceptance criteria are met:

- Authenticated MCP memory service.
- Model-callable read-only relationship queries.
- Offline learned retrieval-weight recommendations.
- Non-loopback or write-enabled viewer.
- Cross-profile federation.
- Embedding or cross-encoder backends.
- Automated relationship extraction on the live response path.

None of these should block the safe integration of the demo's immediately
useful diagnostics, scoped-bank, deduplication, batch, and graph concepts.
