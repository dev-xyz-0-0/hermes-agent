# Phase 0 — Read-only Architecture and Algorithm Audit

## Objective
Map the fork's real interfaces and extract only relevant upstream algorithms without modifying local Git metadata or source.

## Steps
1. Record `git rev-parse HEAD`, `git status --short`, Python/runtime/test tooling, and active branch; do not change anything.
2. Inspect fork files for compressor fields (`context_length`, `threshold_tokens`, `last_prompt_tokens`, `compression_count`, reset hooks), prompt construction, provider usage, session persistence, gateway command routing, CLI, `/new`, `/reset`, warning emission, Holographic prefetch injection.
3. Inspect upstream `agent/context_breakdown.py`, `agent/usage_anchor.py`, `agent/context_compressor.py`, gateway command dispatch and `gateway/run_turn_hygiene.py` via read-only GitHub URLs. Record URL, revision if available, and algorithm contracts. If unreachable, report gap; do not invent source evidence.
4. Build a matrix: actual fork path, existing API, upstream algorithm, reuse/adapt/skip, risks, tests, confidence.
5. Trace the warning string `97% to compaction` from calculation through delivery, and `/new` through session rebinding/reset. Do not assume root cause.
6. Identify all supported command surfaces and command discovery/help mechanisms.

## Deliverables
- `docs` or report-only compatibility matrix (no file edits in this phase)
- Data-flow and reset/warning sequence sketches
- Phase 1 exact file targets and risk register
- Evidence of available tests and commands

## Acceptance
No modifications, fetches, Git refs, restarts, or installations. Unknowns explicitly identified. STOP for approval.
