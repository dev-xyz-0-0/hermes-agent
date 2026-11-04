Authorization — Phase 5.5A: Fork-Native Fallback Compatibility Audit
Phase 5B is accepted based on its reported implementation and regression results.
Proceed with Phase 5.5A only.
Objective
Determine how to integrate the previously specified upstream-derived stalled-summary fallback and attempt-generation ownership algorithms into the current Hermes fork.
Do not perform upstream research. The required algorithms are already specified in the Phase 5.5 implementation plan.
Audit requirements
1. Inspect the existing compression pipeline, auxiliary compression routing, timeout detection, session ownership, cancellation callbacks, and commit fences.
2. Identify whether Phase 5B's newest-attempt-wins mechanism can be reused for fallback ownership.
3. Verify whether the existing auxiliary compression configuration supports selecting a single fallback route without introducing a new provider registry.
4. Determine how a stalled primary worker can be invalidated while allowing one fallback attempt to run.
5. Identify how the active cancellation fence is published so /stop can cancel the fallback rather than the abandoned primary.
6. Identify every compressor attribute that a late worker could restore or overwrite.
7. Identify how cancellation callbacks are installed and cleared, including whether stale workers can clear newer callbacks.
8. Audit pre-compression callbacks and memory notifications for repeated side effects.
9. Determine whether compression-specific route pinning can use contextvars safely across the fork's worker boundaries.
10. Confirm the Phase 5B retry budgets and design a unified maximum of two summary calls per logical compression:
    - Initial summary call: one.
    - Either overflow recovery or stalled-summary fallback: one.
    - Never both.
11. Identify how fallback metadata can be exposed through /context diagnose without invoking providers or changing persisted session schemas.
12. Review compatibility with /new, /reset, /stop, and manual /compress.
Deliverables
Produce:
- File-by-file compatibility matrix.
- Existing mechanisms to reuse.
- Missing mechanisms requiring implementation.
- Proposed fallback state machine.
- Attempt and cancellation ownership design.
- Unified retry-budget design.
- Callback idempotency assessment.
- Deterministic test plan.
- Specific risks and approval decisions.
Constraints
Read-only audit only.
Do not edit source files, tests, configuration, or documentation.
Do not fetch upstream Git history, add remotes, or merge upstream.
Do not install packages, commit, push, restart, or deploy.
Do not modify Phase 6 Holographic Memory Attribution.
Stop after the audit and wait for approval before Phase 5.5B implementation.