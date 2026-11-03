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

## Objective

Implement Phase 2 of the Autonomous Skill Creation project, building directly on the completed P1 `skills.create_dir` implementation.

The goal is to introduce persistent, isolated skill drafts and a strict validation pipeline before skills can be published.

Do not implement Phase 3 publication or Git integration.

## Step 0 — Review Phase 1

Before making changes:

1. Read `AGENTS.md`, `architecture.md`, `phase-02-drafts-validation.md` and `acceptance-checklist.md` from the implementation plan.
2. Inspect `agent/skill_paths.py` and the updated skill discovery implementation.
3. Inspect `tools/skill_manager_tool.py`, `tools/skills_guard.py`, existing configuration loading and relevant tests.
4. Use CodeGraph MCP if available to map dependencies and call sites.
5. Run `git status --short` and identify uncommitted P1 changes.
6. Preserve all existing changes. Do not reset, stash, commit or overwrite without authorization.
7. Establish the current focused-test baseline.

If P1 is still uncommitted, report this before proceeding. Continue only when explicitly authorized to build P2 on top of those changes.

## Step 1 — Draft architecture

Introduce a small, cohesive module under:

```text
agent/skill_creation/
├── __init__.py
├── models.py
├── staging.py
├── validation.py
└── registry.py
```

Adapt filenames to existing repository conventions where appropriate.

Use Python standard-library functionality and existing Hermes dependencies wherever possible.

Avoid introducing a new database or external service.

## Step 2 — Persistent draft storage

Default location:

```text
~/.hermes/skill-drafts/
└── <draft-id>/
    ├── manifest.json
    └── skill/
        ├── SKILL.md
        ├── scripts/
        ├── references/
        └── templates/
```

Requirements:

- Generate unique, unpredictable draft IDs.
- Respect `HERMES_HOME` and active profile configuration.
- Store drafts outside all discoverable skill directories.
- Reject staging roots nested inside skill discovery directories.
- Restrict filesystem permissions where supported.
- Use atomic metadata writes.
- Never follow untrusted symlinks during file operations.
- Enforce file count and size limits.
- Preserve drafts across agent restarts.
- Ensure incomplete writes cannot appear as valid drafts.

The draft registry should be reconstructable from individual draft manifests.

## Step 3 — Lifecycle state machine

Implement:

- `draft`
- `validating`
- `validated`
- `awaiting_approval`
- `rejected`
- `failed`
- `cancelled`

Reserve `published` for Phase 3.

Enforce valid state transitions. A draft cannot become validated without successful validation.

Any modification to draft content must invalidate its previous validation status.

Record timestamps, content hashes, destination metadata and validation results.

Do not store private keys, API keys, authentication tokens or unnecessary conversation content.

## Step 4 — Validation pipeline

Implement reusable validators for:

### Structure

- Valid SKILL.md.
- Valid YAML frontmatter.
- Required metadata fields.
- Existing Hermes naming conventions.
- Maximum file and content sizes.
- Valid supporting-file paths.

Reuse existing skill-manager validation where possible.

### Destination

- Resolve the proposed destination through P1's path resolver.
- Validate category and skill names.
- Detect collisions with existing skills.
- Reject traversal and unsafe symlinks.
- Do not create the destination directory during validation.

### Security

Integrate with the existing `tools/skills_guard.py`.

Requirements:

- Fail closed when the scanner is unavailable.
- Fail closed when the scanner crashes.
- Block high-risk findings.
- Detect obvious credential exposure.
- Treat generated scripts as untrusted.
- Never execute generated scripts during ordinary validation.
- Preserve detailed validation results without exposing secrets.

### Dependencies

Inspect declared commands, packages and MCP dependencies.

Distinguish:

- Available dependency
- Missing dependency
- Unverifiable dependency
- Optional dependency

Do not install packages or execute commands automatically.

### Duplicates

Detect exact name collisions and duplicate content hashes.

Leave semantic duplicate detection for Phase 4.

## Step 5 — Tool integration

Extend the existing `skill_manage` interface with:

- `draft`
- `validate`
- `preview`
- `discard`
- `drafts_list`

Preserve existing actions and return formats.

Expected behavior:

`draft`: Create an isolated unpublished draft.

`validate`: Validate a draft and persist results.

`preview`: Return metadata, validation results and proposed content.

`discard`: Remove an authorized draft safely.

`drafts_list`: Return draft summaries without leaking content from other profiles or users.

Reuse the existing tool registration and permission model.

Do not create a parallel skill-management tool unless necessary.

## Step 6 — Concurrency and integrity

Implement safeguards for:

- Concurrent draft creation.
- Concurrent validation.
- Draft deletion during validation.
- Interrupted metadata writes.
- Modified files after validation.
- Corrupted manifests.
- Stale validation results.
- Unauthorized draft access.

Use content hashes covering all relevant draft files, not SKILL.md alone.

Validate paths again at operation time to reduce time-of-check/time-of-use risks.

## Step 7 — Tests

Create or extend tests for:

```text
tests/agent/test_skill_draft_registry.py
tests/agent/test_skill_draft_validation.py
tests/tools/test_skill_draft_actions.py
tests/agent/test_skill_draft_security.py
```

Cover:

1. Draft creation and persistence.
2. Profile isolation.
3. Discovery exclusion.
4. Valid lifecycle transitions.
5. Invalid transition rejection.
6. Valid SKILL.md.
7. Malformed frontmatter.
8. Missing required metadata.
9. Duplicate names.
10. Unsafe paths and symlinks.
11. Missing dependencies.
12. Scanner unavailable.
13. Scanner exception.
14. High-risk security findings.
15. Content modification invalidates validation.
16. Concurrent operations.
17. Corrupted manifests.
18. Interrupted writes.
19. Draft discard.
20. Legacy skill creation regression.

Use temporary Hermes homes and mock external dependencies.

Do not modify production skill directories.

## Step 8 — Documentation

Update the relevant README and configuration documentation.

Explain:

- Difference between draft and published skill.
- Draft storage location.
- Validation statuses.
- Supported tool actions.
- Security behavior.
- How P2 prepares for Phase 3.

Do not document future functionality as already implemented.

## Step 9 — Verification

Run focused tests and existing skill-manager regression tests.

Then run the full available test suite.

Compare any failures against the established P1 baseline.

Run lint checks on changed files.

Do not claim a test passed unless executed.

## Step 10 — Handoff report

Return:

1. Implementation summary.
2. Files added and modified.
3. Draft lifecycle architecture.
4. Tool action examples.
5. Validation and security behavior.
6. Test results.
7. Existing failures versus new regressions.
8. Known limitations.
9. Phase 3 integration points.
10. Suggested commit message.

Do not commit or push unless explicitly authorized.

Stop after Phase 2. Do not proceed to Phase 3.

## Acceptance criteria

Phase 2 is complete when:

- Drafts persist safely across restarts.
- Drafts remain invisible to normal skill discovery.
- Existing skill creation remains backward compatible.
- Validation is deterministic and fail-closed for required security checks.
- Invalid drafts cannot reach validated status.
- Modified drafts lose their validated status.
- Profile isolation and permissions are enforced.
- All new focused tests pass.
- No unexplained regression is introduced.
