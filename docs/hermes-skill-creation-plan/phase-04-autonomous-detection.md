# Phase 4 — Autonomous workflow candidate detection

## Prerequisite

Phases 1–3 accepted.

## Goal

Suggest reusable skills based on verified repeated successful workflows; never auto-publish.

## Tasks

1. Identify existing agent lifecycle hooks after completed tasks. Do not block user responses or introduce an always-running LLM loop.
2. Collect minimal, sanitized workflow evidence; do not store credentials, private keys, raw sensitive outputs or arbitrary tool payloads.
3. Define evidence of success (user confirmation, successful observable outcome, trusted completion), not model assertion alone.
4. Implement deterministic candidate score: 0.30 repetition + 0.25 reusability + 0.20 verified success + 0.15 complexity + 0.10 evidence completeness. Normalize components to 0–1; calibrate thresholds.
5. Suggested defaults: min_repetitions=3; suggest at score >=0.70; optional draft proposal at >=0.85 only when authorized.
6. Deduplicate by canonical name, content/workflow fingerprint, pending drafts, existing skills and rejected candidates. Semantic matching may supplement but not override deterministic checks.
7. Enforce max 3 suggestions/day, 1/session, rejection cooldown and profile isolation.
8. Expose candidate inspection, accept, dismiss and suppression actions using existing UI patterns.
9. Route accepted candidate through Phase 2 drafts and Phase 3 approval; never bypass checks.
10. Default disabled; mode=suggest when enabled.

## Tests

- Three verified successes produce one candidate.
- Failed workflows do not count as successes.
- Existing matching skill prevents duplicate suggestion.
- Rejected suggestion suppressed.
- Disabled mode emits nothing.
- Candidate evaluation does not delay user response.
- Restart retains candidate state and quotas.

## Gate

Repeated successful task evidence creates a single suggestion; accepting it creates a draft; publishing still requires validation and separate approval.

## Handoff

Document signals, score calibration, false-positive rate from sample sessions, privacy controls and test results.
