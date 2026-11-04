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
