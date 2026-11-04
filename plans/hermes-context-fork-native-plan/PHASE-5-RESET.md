# Phase 5 — Session Reset and 97% Warning Diagnostics (P1)

## Objective
Reproduce and explain the persistent `97% to compaction` warning after `/new`; add `/context diagnose` with evidence-based statuses.

## Investigation
1. Trace warning generation, queueing, formatting, and delivery, and `/new` session rotation.
2. Test three hypotheses independently: stale accounting, reused session, delayed warning from old session; also measure legitimate new-session system/tool overhead.
3. Add session generation/identity to diagnostic warning metadata only if existing session identity is insufficient.
4. On delivery, reject or correctly label warnings belonging to prior sessions; preserve valid warnings for other viewers still attached to prior sessions.
5. Fix only demonstrated bug with minimal scope; do not change compaction threshold or rewrite `/new` wholesale.

## `/context diagnose`
Report current session (safe short ID), measurement source and freshness, effective threshold, last reset when known, last warning ownership, and PASS/FAIL/UNKNOWN per check. Never invent success or expose raw messages.

## Tests
Reach 97% in session A; issue `/new` -> B; inject delayed A warning; ensure B is not shown A's warning; ensure other viewers of A retain correct semantics; verify B counters and session history; concurrent turn/reset; repeated reset; gateway restart.

## Deliverables and gate
Root-cause report, failing-before/passing-after regression, minimal fix, `/context diagnose`. STOP for approval.

## Additional Phase 5 Requirements — Compaction Reliability

The upstream research has already been completed. Do not perform additional upstream research or fetch upstream Git history.

Implement the following fork-native behavior alongside the existing Phase 5 reset and `/context diagnose` work:

1. Remove explicit hardcoded temperature from compression-specific summary requests, preserving global provider settings.

2. Add or reuse a minimal summary-failure classifier for authentication/quota, network, timeout, overload, empty output, truncated output, and context overflow.

3. Implement a session-isolated, monotonic-clock cooldown that suppresses repeated failed summary LLM calls. Do not introduce persistent cooldowns or adaptive backoff in this phase.

4. Ensure empty and truncated summaries are never committed as valid summaries. Preserve the original transcript for terminal summary failures. If this conflicts with the existing provider-overflow recovery mechanism, stop and report the conflict before changing that behavior.

5. Preserve bounded manual `/compress` recovery. A manual retry may bypass the ordinary cooldown but must not create an unbounded retry loop.

6. Extend read-only `/context diagnose` with last summary failure classification, cooldown status, and last compaction outcome where the information already exists.

7. Ensure `/new` and `/reset` cannot inherit failure state or queued warnings belonging to a previous session.

8. Ensure successful transcript compaction invalidates or rebinds usage anchors correctly. Failed, non-mutating compaction must not unnecessarily invalidate a valid anchor.

9. Add deterministic tests for temperature-restricted models, summary failures, cooldown behavior, manual recovery, reset races, anchor validity, and successful compaction.

Do not implement persistent cooldowns, adaptive backoff, sustained-overload degradation, or new summarization architecture.

Preserve Phase 1–4 APIs, compaction thresholds, Holographic memory behavior, and existing persistence schemas.

Follow the original Phase 5 approval boundary: complete and report Phase 5A first. Stop for approval before any change to transcript-loss or overflow-recovery policy.

Do not commit, push, restart, or deploy.
