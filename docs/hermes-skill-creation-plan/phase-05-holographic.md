# Phase 5 — Optional Holographic memory integration

## Prerequisite

Phases 1–4 accepted.

## Goal

Use Holographic memory to recall successful workflow evidence and user preferences without making memory the authoritative skill registry.

## Tasks

1. Inspect Holographic provider API, namespace resolution, prefetch lifecycle and storage boundaries in current fork.
2. Add provider-neutral candidate evidence adapter with no-memory fallback (local state remains authoritative for drafts/approvals/publication).
3. Store only bounded, sanitized workflow summaries, fingerprints, references, timestamps and provenance; never secrets, private keys or executable authority.
4. Keep Telegram/chat/profile namespace isolation and honor memory trust/forget semantics.
5. Retrieve relevant prior workflow evidence to improve candidate scoring/deduplication, with provenance and confidence checks.
6. Avoid recursive auto-capture of candidate/skill metadata into endless duplicate facts.
7. Add bounded recall budget, cooldown and failure fallback; do not block main agent loop.
8. Never let memory contents grant permission to create, execute, approve, commit or publish.
9. Document diagnostics and opt-in configuration.

## Tests

- No provider -> functional local candidate suggestions.
- Provider unavailable -> graceful fallback.
- Namespace isolation across profiles/chats.
- Forgotten/rejected evidence not resurrected.
- Malicious memory content cannot authorize publication.
- Candidate state and published Git files remain consistent across restart.
- Recall limits and deduplication work.

## Gate

Memory-assisted candidate discovery improves recall without altering authorization, publication state or isolation. Full regressions pass.

## Handoff

Document provider API use, data retention, diagnostics, fallback, isolation tests and remaining risks.
