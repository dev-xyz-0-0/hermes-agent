# Phase 6 — Holographic Recall Attribution (P2)

## Objective
Report the token overhead of dynamically injected Holographic recall without exposing memory content or modifying the provider.

## Implementation
1. Locate fork's Holographic prefetch/cache and API-call-time memory injection; identify measurement point at provider-visible prompt construction.
2. Count tokens from the exact injected block when available; otherwise report estimate/unavailable with provenance.
3. Distinguish static memory/profile content from dynamic recall and prevent double counting against base system or conversation categories.
4. Scope measurements by platform/chat/session and current turn; do not query or alter memory DB, trust scores, or retrieval counters solely for diagnostics.
5. Support disabled provider, no recall, recall cache refresh, multiple calls per turn, and prompt truncation.

## Tests
Memory enabled/disabled, zero hits, multiple hits, turn changes, repeated `/context` does not increase retrieval_count, cross-chat isolation, no facts/PII in output, no extra DB reads.

## Deliverables and gate
Read-only attribution adapter, focused tests, explicit limitations. STOP for approval.
