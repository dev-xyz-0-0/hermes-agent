# Phase 7 — Documentation, Verification, and Controlled Rollout

## Objective
Close the feature with evidence, not assumptions.

## Tasks
1. Update README/command docs and `/commands` help for `/context`, `/ctx`, `/context all`, `/context diagnose` if implemented.
2. Document context-window percent vs compaction-threshold percent, measurement provenance, unknown values, attribution limitations, and reset behavior.
3. Run targeted tests, then appropriate fork suite; record exact commands and failures, including pre-existing failures.
4. Review diffs for secrets, unintended config changes, duplicate counters, blocking I/O, command injection, session leakage, and gateway message size.
5. Verify backward compatibility of existing `/new`, `/reset`, `/status`, memory and compressor behavior.
6. Produce rollout and rollback instructions; no service restart, deployment, commit, or push without approval.

## Acceptance
P0 commands functional and documented; reset race covered; no LLM call or mutation from diagnostics; correct unknown handling; test evidence; unresolved issues listed. STOP for release approval.
