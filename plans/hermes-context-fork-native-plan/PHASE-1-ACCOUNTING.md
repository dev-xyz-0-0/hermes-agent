# Phase 1 — Context Occupancy and Pressure Engine (P0)

## Objective
Create a small, pure, read-only diagnostics snapshot API adapted to the fork.

## Implementation
1. Reuse existing model metadata, compressor state, and provider usage counters; avoid second source of truth.
2. Define typed immutable snapshot structures for model/window, used tokens, measurement provenance/freshness, effective threshold, pressure, remaining budget, session key, and limitations.
3. Implement precedence: valid current provider prompt usage -> valid existing anchor -> fork-compatible local estimate -> unavailable. Do not build anchor persistence here.
4. Implement pressure math in `ALGORITHMS.md`; keep window utilization and compaction pressure separate.
5. Handle missing/zero window, unknown model, threshold edge cases, stale counters, provider changes, and prompt modifications.
6. Do not call LLM or mutate session/compressor state. Do not treat lifetime usage as occupancy.

## Tests
Known example W=200000, B=100000, U=97000 => 48.5% window, 97% compaction, 3000 remaining; over-threshold; invalid inputs; provider-vs-estimate provenance; no side effects; no session cross-talk.

## Deliverables and gate
Minimal fork-native module or reuse existing module; targeted tests; diff and test report. STOP for approval.
