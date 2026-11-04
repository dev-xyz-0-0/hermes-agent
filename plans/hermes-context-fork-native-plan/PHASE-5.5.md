# Phase 5.5 — Fork-Native Compression Fallback & Attempt Ownership

## Objective

Implement bounded recovery for stalled compression summaries and concurrency-safe attempt ownership in the Hermes Agent fork.

The implementation must reproduce the core algorithms established by upstream PR #96634 and PR #96949 while preserving the fork's existing architecture.

This phase is independent of Phase 6 — Holographic Memory Attribution. Do not rename or modify Phase 6.

## Prerequisites

Verify Phase 5B has completed its safe-compaction contract:

- Failed summary generation preserves the original transcript.
- Empty or explicitly truncated summaries are rejected.
- Failed attempts do not report successful compaction.
- Successful compaction commits only after validation.
- Failed, non-mutating attempts preserve valid usage anchors.
- Existing cooldown and manual `/compress` behavior remain bounded.

If these prerequisites are missing, stop and report them. Do not silently implement Phase 5B.

## 5.5A — Architecture Compatibility Audit

Inspect the existing fork implementations of:

- `agent/context_compressor.py`
- `agent/conversation_compression.py`, if present
- `agent/auxiliary_client.py`
- `run_agent.py`
- `gateway/run.py`
- Existing compression configuration and fallback routing
- Existing cancellation and commit-fence mechanisms
- Phase 5 `/context diagnose`
- Relevant tests

Identify which existing interfaces can support:

1. Progress-aware stall detection.
2. Compression-specific fallback route resolution.
3. Single-use route pinning.
4. Detached-worker cancellation.
5. Fresh commit-fence publication.
6. Per-compressor attempt generations.
7. Owner-stamped cancellation callbacks.

Produce a brief file-by-file compatibility matrix before editing.

Do not introduce duplicate routing, timeout, or cancellation infrastructure where existing mechanisms are sufficient.

## 5.5B — Bounded Stall-Fallback Algorithm

Implement the following state machine:

PRIMARY_RUNNING
  -> PRIMARY_SUCCESS: validate and commit
  -> PRIMARY_STALLED: revoke primary commit eligibility
  -> EXPLICIT_CANCEL: abort without fallback

PRIMARY_STALLED
  -> NO_FALLBACK: preserve transcript and report failure
  -> FALLBACK_READY: create fresh commit fence

FALLBACK_READY
  -> FALLBACK_RUNNING: invoke one pinned fallback route

FALLBACK_RUNNING
  -> FALLBACK_SUCCESS: validate ownership, summary, and commit fence; commit once
  -> FALLBACK_FAILURE: preserve transcript and report failure
  -> FALLBACK_STALLED: preserve transcript and report failure
  -> CANCELLED: abort without commit

Requirements:

1. Reuse the existing progress-aware timeout mechanism.
2. Treat a connected but non-progressing summary stream as a stall.
3. Do not mistake a progressing request for a stalled request.
4. Resolve the first eligible route from the existing compression fallback configuration.
5. Skip malformed or incomplete route entries.
6. Resolve provider/model/API mode, credentials, and timeout using existing helpers.
7. Do not log credentials, tokens, or raw authenticated endpoints.
8. Retry at most once after a detected primary stall.
9. Do not recursively traverse fallback routes.
10. Do not retry after an explicit `/stop`.
11. Run the fallback before the primary timeout callback activates failure cooldown.
12. Ensure final failure accounting and callbacks occur exactly once.
13. Preserve the transcript when both attempts fail.

Keep normal auxiliary-client exception-based fallback behavior intact.

Do not broaden this feature into retries for all provider failures.

## 5.5C — Single-Use Route Pinning

Implement a context-local route pin equivalent to upstream's `pin_summary_route` and `take_pinned_summary_route`.

Required semantics:

- Pin applies only to the intended fallback compression summary call.
- Pin is consumed exactly once.
- Pin propagates across the appropriate worker boundary.
- Pin does not leak into detached primary workers.
- Pin does not leak into unrelated sessions or concurrent attempts.
- Pin does not force subsequent main-model retries onto the fallback route.
- Pin is cleared when its attempt exits.

Use `contextvars` if compatible with the fork's worker architecture. Otherwise use an equally isolated existing mechanism.

For optional chunk-digest generation, retain a non-consuming context-local reference to the consumed route so related digest calls can follow the healthy fallback backend.

Do not reuse the single-use summary pin for digest calls.

## 5.5D — Commit-Fence Publication

A stalled primary worker may remain alive after its commit eligibility is revoked.

The fallback therefore requires a new commit fence.

Requirements:

1. Revoke the primary attempt's commit eligibility.
2. Mint a fresh fallback fence through the existing host factory.
3. Publish the fallback fence to the active cancellation/interrupt control plane.
4. Ensure `/stop` observes the fallback fence rather than the abandoned primary fence.
5. Revalidate session ownership before committing.
6. Prevent an abandoned primary from committing after fallback begins.
7. Restore or clear active-fence state only when the acting attempt still owns it.

If the host fence factory is unavailable, fails, or cannot publish the new fence, fail safely and report the limitation.

Do not silently run a fallback using a private fence invisible to `/stop`.

## 5.5E — Per-Compressor Attempt Generations

Implement the core ownership algorithm from PR #96949.

Maintain a monotonic generation for each compressor instance.

Each primary, fallback, or subsequent compression attempt claims a new generation under a synchronization lock.

Conceptual interfaces:

- `claim_compressor_attempt(compressor)`
- `restore_attempt_state(compressor, snapshot, generation)`
- `install_cancelled_check(compressor, callback, generation)`
- `clear_cancelled_check_if_owner(compressor, generation)`

Adapt naming and interfaces to the fork.

### State restoration

A snapshot rollback may restore compressor attributes only if the restoring attempt still owns the current generation.

A stale primary rollback must be a no-op.

Generation validation and in-memory attribute restoration must execute under the same lock used to claim generations.

Do not perform a separate unlocked staleness check followed by attribute writes.

Avoid holding this lock during provider calls, blocking database I/O, or other slow operations.

### Cancellation callback ownership

Stamp each installed cancellation callback with the owning generation.

Only that generation may clear or replace its own callback according to the active-attempt transition rules.

An abandoned primary worker's `finally` block must not clear the fallback worker's callback.

### Separate ownership boundaries

Preserve two independent safeguards:

- Commit fence: authorizes transcript commit.
- Attempt generation: authorizes compressor state mutation.

Neither mechanism substitutes for the other.

### Compatibility

Preserve existing behavior for legitimate same-attempt rollback.

Handle custom compressor implementations that cannot store new attributes without introducing unexpected exceptions.

If compatibility requires disabling generation guards for unsupported third-party compressors, document the reduced guarantee and ensure the built-in compressor remains protected.

## 5.5F — Session and Cancellation Lifecycle

Integrate with:

- `/new`
- `/reset`
- `/stop`
- Manual `/compress`
- Gateway session rebinding
- Active-turn cancellation

Requirements:

1. Session changes invalidate obsolete compression attempts.
2. Stale workers cannot commit to a replacement session.
3. Explicit stop prevents fallback initiation.
4. Stop during fallback prevents commit.
5. A late worker cannot clear a newer session's cancellation state.
6. A failed fallback preserves the original transcript and valid usage anchors.
7. A successful transcript rewrite invalidates or rebinds anchors according to Phase 4 rules.
8. Existing Phase 5 warning-ownership behavior remains unchanged.

Do not change session persistence schemas or Holographic memory behavior.

## 5.5G — Callback Idempotency

Upstream acknowledges that rerunning the compression worker can repeat pre-compression callbacks.

Inspect all callbacks executed before summary generation.

Classify them as:

- Pure/read-only
- Idempotent mutation
- Potentially non-idempotent side effect

Do not assume custom callbacks are idempotent.

Ensure fallback retry does not repeat non-idempotent effects such as external writes, user notifications, or irreversible tool operations.

Reuse previously completed preparation where safe, or stop and report an architectural conflict if the current pipeline cannot avoid duplicate side effects.

Do not introduce a broad callback-system redesign.

## 5.5H — Diagnostics

Extend `/context diagnose` using existing read-only snapshots.

Expose, where available:

- Compression attempt state
- Primary route status
- Fallback route selected
- Fallback attempt count
- Attempt generation
- Commit-fence status
- Last failure classification
- Last fallback outcome
- Transcript preserved or committed
- Cancellation state

Use safe provider/model identifiers only.

Never expose credentials or raw authenticated endpoints.

Diagnostics must not initiate compression, resolve providers, rebuild prompts, query memory, or invoke an LLM.

Keep output bounded for CLI and Telegram.

## 5.5I — Deterministic Regression Tests

Implement focused tests for:

### Routing
- Primary success without fallback
- Zero-progress primary stall
- Slow but progressing primary
- Missing fallback chain
- Invalid fallback entry
- First eligible fallback selection
- Successful fallback
- Failed fallback
- Stalled fallback
- Exactly one retry
- No recursive retry
- Single-use route pin
- Route context isolation
- Digest calls following consumed fallback route

### Ownership
- Late primary completes after fallback
- Late primary attempts snapshot restoration
- Stale restore cannot overwrite fallback state
- Generation check and write are atomic
- Same-generation rollback remains valid
- Stale primary clears cancellation callback
- New fallback callback remains installed
- Old worker cannot replace active fence
- Duplicate completion callback cannot commit twice

### Cancellation
- `/stop` before fallback
- `/stop` during fallback
- `/new` during fallback
- `/reset` during fallback
- Cross-session isolation
- Concurrent compression attempts

### Safety
- Failed fallback preserves transcript
- Successful fallback commits once
- Invalid summary never commits
- Usage anchors preserved on non-mutating failure
- Usage anchors invalidated after rewrite
- No duplicate non-idempotent callbacks
- Cooldown activated only according to final outcome
- Manual `/compress` remains bounded
- Diagnostics remain read-only

Use synchronization events, barriers, controlled futures, and fake providers.

Avoid sleep-dependent race tests.

Run the Phase 1–5 regression suites and existing gateway reset/session tests.

Investigate and report the previously observed asynchronous teardown timeouts separately from functional assertion failures.

## Constraints

- Do not modify Phase 6 — Holographic Memory Attribution.
- Do not implement Phase 7.
- Do not fetch upstream Git history or add Git remotes.
- Do not wholesale-copy upstream modules.
- Do not change context-window calculations or compaction thresholds.
- Do not introduce persistent cooldowns or adaptive backoff.
- Do not introduce new database tables or migrations.
- Do not change global provider temperature settings.
- Do not commit, push, restart, or deploy.

## Approval Boundaries

5.5A: Complete compatibility audit and stop for review before code changes.

5.5B–5.5G: Implement the approved fallback and ownership mechanisms after approval.

5.5H–5.5I: Complete diagnostics and tests.

Stop for approval if an implementation requires changing Phase 5B transcript-preservation policy, overflow-recovery policy, or non-idempotent callback semantics.

## Deliverables

Report:

1. Compatibility matrix.
2. Modified files.
3. Stall-detection algorithm.
4. Fallback route-resolution design.
5. Single-use pin implementation.
6. Commit-fence publication and cancellation handling.
7. Attempt-generation ownership and locking.
8. Callback idempotency findings.
9. `/context diagnose` output.
10. Regression results and unresolved failures.# Phase 5.5 — Fork-Native Compression Fallback & Attempt Ownership

## Objective

Implement bounded recovery for stalled compression summaries and concurrency-safe attempt ownership in the Hermes Agent fork.

The implementation must reproduce the core algorithms established by upstream PR #96634 and PR #96949 while preserving the fork's existing architecture.

This phase is independent of Phase 6 — Holographic Memory Attribution. Do not rename or modify Phase 6.

## Prerequisites

Verify Phase 5B has completed its safe-compaction contract:

- Failed summary generation preserves the original transcript.
- Empty or explicitly truncated summaries are rejected.
- Failed attempts do not report successful compaction.
- Successful compaction commits only after validation.
- Failed, non-mutating attempts preserve valid usage anchors.
- Existing cooldown and manual `/compress` behavior remain bounded.

If these prerequisites are missing, stop and report them. Do not silently implement Phase 5B.

## 5.5A — Architecture Compatibility Audit

Inspect the existing fork implementations of:

- `agent/context_compressor.py`
- `agent/conversation_compression.py`, if present
- `agent/auxiliary_client.py`
- `run_agent.py`
- `gateway/run.py`
- Existing compression configuration and fallback routing
- Existing cancellation and commit-fence mechanisms
- Phase 5 `/context diagnose`
- Relevant tests

Identify which existing interfaces can support:

1. Progress-aware stall detection.
2. Compression-specific fallback route resolution.
3. Single-use route pinning.
4. Detached-worker cancellation.
5. Fresh commit-fence publication.
6. Per-compressor attempt generations.
7. Owner-stamped cancellation callbacks.

Produce a brief file-by-file compatibility matrix before editing.

Do not introduce duplicate routing, timeout, or cancellation infrastructure where existing mechanisms are sufficient.

## 5.5B — Bounded Stall-Fallback Algorithm

Implement the following state machine:

PRIMARY_RUNNING
  -> PRIMARY_SUCCESS: validate and commit
  -> PRIMARY_STALLED: revoke primary commit eligibility
  -> EXPLICIT_CANCEL: abort without fallback

PRIMARY_STALLED
  -> NO_FALLBACK: preserve transcript and report failure
  -> FALLBACK_READY: create fresh commit fence

FALLBACK_READY
  -> FALLBACK_RUNNING: invoke one pinned fallback route

FALLBACK_RUNNING
  -> FALLBACK_SUCCESS: validate ownership, summary, and commit fence; commit once
  -> FALLBACK_FAILURE: preserve transcript and report failure
  -> FALLBACK_STALLED: preserve transcript and report failure
  -> CANCELLED: abort without commit

Requirements:

1. Reuse the existing progress-aware timeout mechanism.
2. Treat a connected but non-progressing summary stream as a stall.
3. Do not mistake a progressing request for a stalled request.
4. Resolve the first eligible route from the existing compression fallback configuration.
5. Skip malformed or incomplete route entries.
6. Resolve provider/model/API mode, credentials, and timeout using existing helpers.
7. Do not log credentials, tokens, or raw authenticated endpoints.
8. Retry at most once after a detected primary stall.
9. Do not recursively traverse fallback routes.
10. Do not retry after an explicit `/stop`.
11. Run the fallback before the primary timeout callback activates failure cooldown.
12. Ensure final failure accounting and callbacks occur exactly once.
13. Preserve the transcript when both attempts fail.

Keep normal auxiliary-client exception-based fallback behavior intact.

Do not broaden this feature into retries for all provider failures.

## 5.5C — Single-Use Route Pinning

Implement a context-local route pin equivalent to upstream's `pin_summary_route` and `take_pinned_summary_route`.

Required semantics:

- Pin applies only to the intended fallback compression summary call.
- Pin is consumed exactly once.
- Pin propagates across the appropriate worker boundary.
- Pin does not leak into detached primary workers.
- Pin does not leak into unrelated sessions or concurrent attempts.
- Pin does not force subsequent main-model retries onto the fallback route.
- Pin is cleared when its attempt exits.

Use `contextvars` if compatible with the fork's worker architecture. Otherwise use an equally isolated existing mechanism.

For optional chunk-digest generation, retain a non-consuming context-local reference to the consumed route so related digest calls can follow the healthy fallback backend.

Do not reuse the single-use summary pin for digest calls.

## 5.5D — Commit-Fence Publication

A stalled primary worker may remain alive after its commit eligibility is revoked.

The fallback therefore requires a new commit fence.

Requirements:

1. Revoke the primary attempt's commit eligibility.
2. Mint a fresh fallback fence through the existing host factory.
3. Publish the fallback fence to the active cancellation/interrupt control plane.
4. Ensure `/stop` observes the fallback fence rather than the abandoned primary fence.
5. Revalidate session ownership before committing.
6. Prevent an abandoned primary from committing after fallback begins.
7. Restore or clear active-fence state only when the acting attempt still owns it.

If the host fence factory is unavailable, fails, or cannot publish the new fence, fail safely and report the limitation.

Do not silently run a fallback using a private fence invisible to `/stop`.

## 5.5E — Per-Compressor Attempt Generations

Implement the core ownership algorithm from PR #96949.

Maintain a monotonic generation for each compressor instance.

Each primary, fallback, or subsequent compression attempt claims a new generation under a synchronization lock.

Conceptual interfaces:

- `claim_compressor_attempt(compressor)`
- `restore_attempt_state(compressor, snapshot, generation)`
- `install_cancelled_check(compressor, callback, generation)`
- `clear_cancelled_check_if_owner(compressor, generation)`

Adapt naming and interfaces to the fork.

### State restoration

A snapshot rollback may restore compressor attributes only if the restoring attempt still owns the current generation.

A stale primary rollback must be a no-op.

Generation validation and in-memory attribute restoration must execute under the same lock used to claim generations.

Do not perform a separate unlocked staleness check followed by attribute writes.

Avoid holding this lock during provider calls, blocking database I/O, or other slow operations.

### Cancellation callback ownership

Stamp each installed cancellation callback with the owning generation.

Only that generation may clear or replace its own callback according to the active-attempt transition rules.

An abandoned primary worker's `finally` block must not clear the fallback worker's callback.

### Separate ownership boundaries

Preserve two independent safeguards:

- Commit fence: authorizes transcript commit.
- Attempt generation: authorizes compressor state mutation.

Neither mechanism substitutes for the other.

### Compatibility

Preserve existing behavior for legitimate same-attempt rollback.

Handle custom compressor implementations that cannot store new attributes without introducing unexpected exceptions.

If compatibility requires disabling generation guards for unsupported third-party compressors, document the reduced guarantee and ensure the built-in compressor remains protected.

## 5.5F — Session and Cancellation Lifecycle

Integrate with:

- `/new`
- `/reset`
- `/stop`
- Manual `/compress`
- Gateway session rebinding
- Active-turn cancellation

Requirements:

1. Session changes invalidate obsolete compression attempts.
2. Stale workers cannot commit to a replacement session.
3. Explicit stop prevents fallback initiation.
4. Stop during fallback prevents commit.
5. A late worker cannot clear a newer session's cancellation state.
6. A failed fallback preserves the original transcript and valid usage anchors.
7. A successful transcript rewrite invalidates or rebinds anchors according to Phase 4 rules.
8. Existing Phase 5 warning-ownership behavior remains unchanged.

Do not change session persistence schemas or Holographic memory behavior.

## 5.5G — Callback Idempotency

Upstream acknowledges that rerunning the compression worker can repeat pre-compression callbacks.

Inspect all callbacks executed before summary generation.

Classify them as:

- Pure/read-only
- Idempotent mutation
- Potentially non-idempotent side effect

Do not assume custom callbacks are idempotent.

Ensure fallback retry does not repeat non-idempotent effects such as external writes, user notifications, or irreversible tool operations.

Reuse previously completed preparation where safe, or stop and report an architectural conflict if the current pipeline cannot avoid duplicate side effects.

Do not introduce a broad callback-system redesign.

## 5.5H — Diagnostics

Extend `/context diagnose` using existing read-only snapshots.

Expose, where available:

- Compression attempt state
- Primary route status
- Fallback route selected
- Fallback attempt count
- Attempt generation
- Commit-fence status
- Last failure classification
- Last fallback outcome
- Transcript preserved or committed
- Cancellation state

Use safe provider/model identifiers only.

Never expose credentials or raw authenticated endpoints.

Diagnostics must not initiate compression, resolve providers, rebuild prompts, query memory, or invoke an LLM.

Keep output bounded for CLI and Telegram.

## 5.5I — Deterministic Regression Tests

Implement focused tests for:

### Routing
- Primary success without fallback
- Zero-progress primary stall
- Slow but progressing primary
- Missing fallback chain
- Invalid fallback entry
- First eligible fallback selection
- Successful fallback
- Failed fallback
- Stalled fallback
- Exactly one retry
- No recursive retry
- Single-use route pin
- Route context isolation
- Digest calls following consumed fallback route

### Ownership
- Late primary completes after fallback
- Late primary attempts snapshot restoration
- Stale restore cannot overwrite fallback state
- Generation check and write are atomic
- Same-generation rollback remains valid
- Stale primary clears cancellation callback
- New fallback callback remains installed
- Old worker cannot replace active fence
- Duplicate completion callback cannot commit twice

### Cancellation
- `/stop` before fallback
- `/stop` during fallback
- `/new` during fallback
- `/reset` during fallback
- Cross-session isolation
- Concurrent compression attempts

### Safety
- Failed fallback preserves transcript
- Successful fallback commits once
- Invalid summary never commits
- Usage anchors preserved on non-mutating failure
- Usage anchors invalidated after rewrite
- No duplicate non-idempotent callbacks
- Cooldown activated only according to final outcome
- Manual `/compress` remains bounded
- Diagnostics remain read-only

Use synchronization events, barriers, controlled futures, and fake providers.

Avoid sleep-dependent race tests.

Run the Phase 1–5 regression suites and existing gateway reset/session tests.

Investigate and report the previously observed asynchronous teardown timeouts separately from functional assertion failures.

## Constraints

- Do not modify Phase 6 — Holographic Memory Attribution.
- Do not implement Phase 7.
- Do not fetch upstream Git history or add Git remotes.
- Do not wholesale-copy upstream modules.
- Do not change context-window calculations or compaction thresholds.
- Do not introduce persistent cooldowns or adaptive backoff.
- Do not introduce new database tables or migrations.
- Do not change global provider temperature settings.
- Do not commit, push, restart, or deploy.

## Approval Boundaries

5.5A: Complete compatibility audit and stop for review before code changes.

5.5B–5.5G: Implement the approved fallback and ownership mechanisms after approval.

5.5H–5.5I: Complete diagnostics and tests.

Stop for approval if an implementation requires changing Phase 5B transcript-preservation policy, overflow-recovery policy, or non-idempotent callback semantics.

## Deliverables

Report:

1. Compatibility matrix.
2. Modified files.
3. Stall-detection algorithm.
4. Fallback route-resolution design.
5. Single-use pin implementation.
6. Commit-fence publication and cancellation handling.
7. Attempt-generation ownership and locking.
8. Callback idempotency findings.
9. `/context diagnose` output.
10. Regression results and unresolved failures.
11. Remaining limitations.
12. Confirmation that Phase 6 Holographic is unchanged.
11. Remaining limitations.
12. Confirmation that Phase 6 Holographic is unchanged.