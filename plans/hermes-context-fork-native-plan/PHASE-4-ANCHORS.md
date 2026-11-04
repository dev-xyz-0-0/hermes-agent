# Phase 4 — Usage Anchors and Provenance (P1, Conditional)

## Entry condition
Phase 0–3 evidence shows existing fork accounting cannot accurately estimate context between provider responses. If equivalent functionality exists, document reuse and skip new storage.

## Implementation
1. Define provider usage anchor bound to session, model, context config, provider-visible transcript prefix, and prompt tokens.
2. Compute stable fingerprints of serialized provider-visible message fields, with explicit handling of tool calls, images, and non-text content. Do not hash or store secret data in logs.
3. Validate prefix fingerprint and session/model binding before estimating appended-message delta.
4. Invalidate after `/new`, `/reset`, compaction, transcript rewrites, or incompatible model/provider changes.
5. If persistence is necessary, adapt existing fork session metadata API; version metadata and handle missing/corrupt/old anchors. No new database by default.
6. Prevent reasoning-token and tool-message double counting. Anchor is diagnostic only and must not change compaction decisions without separate approval.

## Tests
Valid append, rewritten prefix, same final message but earlier rewrite, reset, model switch, restart, corrupt metadata, tool and multimodal messages, concurrency, no raw-content persistence.

## Deliverables and gate
Evidence-backed reuse or minimal anchor adapter; provenance tests; migration/rollback notes if metadata changes. STOP for approval.
