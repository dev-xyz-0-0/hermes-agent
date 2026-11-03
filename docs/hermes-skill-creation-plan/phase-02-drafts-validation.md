# Phase 2 — Draft lifecycle and validation

## Prerequisite

Phase 1 accepted and merged or otherwise fixed at a tested commit.

## Goal

Create non-discoverable drafts and validate them safely before publication.

## Tasks

1. Design a profile-scoped draft registry under `~/.hermes/skill-drafts/<draft-id>/` with manifest, skill tree, timestamps, creator/session metadata, target destination and content hashes.
2. Define state transitions: draft, validating, validated, awaiting_approval, rejected, cancelled, failed, published. Use atomic manifest writes and robust restart recovery.
3. Add management actions such as `draft`, `validate`, `preview`, `discard`, `drafts_list` without breaking existing `create` calls. Match actual tool schema patterns.
4. Reuse existing SKILL.md/frontmatter validation, name/category checks and file-size limits.
5. Integrate existing security guard; fail closed if scanner is unavailable or errors. Detect secrets, unsafe commands and suspicious dependencies.
6. Validate paths, symlinks, dependency declarations and exact/near-duplicate names. Semantic duplicate detection may be advisory initially.
7. Never run generated scripts during ordinary validation. Optional sandboxed smoke tests require separate explicit authorization.
8. Emit machine-readable validation report with pass/warning/blocked/error and immutable content digest.
9. Ensure drafts are excluded from discovery and agent invocation.

## Tests

- Draft persists across restart; corrupt manifest safely rejected.
- Draft not listed as published.
- Invalid frontmatter/name/path blocked.
- Scanner exception blocks publication eligibility.
- Content mutation invalidates previous validation digest.
- Secret and traversal cases blocked.
- Existing `create` behavior unchanged.

## Gate

A valid draft validates and is previewable; an invalid draft is blocked; neither is visible as an active skill. No generated scripts execute during validation.

## Handoff

List schema/actions, lifecycle transitions, validation policy, test results, limitations and security findings.
