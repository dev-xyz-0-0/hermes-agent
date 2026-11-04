# Hermes Agent — Fork-Native Context Diagnostics

Target: `dev-xyz-0-0/hermes-agent`. Reference only: `NousResearch/hermes-agent`.

## Goal
Implement `/context`, `/ctx`, `/context all`, and eventually `/context diagnose` by extracting **algorithms**, not importing upstream architecture. Preserve the fork's compressor, gateway, Holographic memory, and session model.

## Hard constraints
- No upstream Git fetch, remotes, refs, merge, cherry-pick, rebase, or reset.
- Upstream source may be inspected using read-only GitHub URLs. Record URL and commit SHA when available; if not, mark unpinned.
- Do not copy entire upstream modules; implement against actual fork interfaces.
- No new dependencies or persistent database by default; no production restart, commit, or push without approval.
- No model calls or conversation mutations from diagnostics.
- Each phase is independently reviewable. Stop at phase boundaries.

## Sequence
0. Architecture and algorithm audit (read-only)
1. Occupancy, pressure, and provenance engine
2. Category attribution and renderers
3. Gateway and CLI commands
4. Usage anchors (conditional)
5. Session reset and stale-warning diagnosis
6. Holographic memory attribution
7. Release, docs, and regression verification

## Priority and dependencies
P0: Phases 0–3. P1: Phases 4–5. P2: Phase 6. Phase 7 closes all work. Phase 4 is conditional: skip redundant anchor implementation when existing fork measurements suffice. Phase 5 should diagnose before applying any fix.

## Definition of done
`/context`, `/ctx`, `/context all` work via supported entrypoints; metrics disclose provenance; no double counting; `/new` diagnostics correctly bind to the new session; warning races covered; no secrets leaked; tests and documentation pass.

## Files in this archive
`CODEX-MASTER-PROMPT.md`, `PHASE-0-AUDIT.md` through `PHASE-7-RELEASE.md`, `TEST-MATRIX.md`, `ALGORITHMS.md`.
