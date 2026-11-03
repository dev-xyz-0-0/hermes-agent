# Phase 3 — Approval, atomic publication and Git

## Prerequisite

Phases 1 and 2 accepted.

## Goal

Publish approved, validated drafts safely to `create_dir`, reload them and optionally commit only intended files.

## Tasks

1. Reuse existing gateway/CLI approval infrastructure where suitable. Bind authorization to user/chat/profile, draft ID, destination, expiry and content hash.
2. Preview exact files, validation report, destination and proposed Git behavior. Reject expired, mismatched or modified drafts.
3. Revalidate target and draft immediately before publishing; enforce no overwrite of existing skill and no symlink escapes.
4. Build a temporary directory on destination filesystem; verify copied content against manifest; atomically rename into place. Add per-target locking and collision handling.
5. Track filesystem publication independently of Git state; support recovery/retry after Git failure.
6. Refresh skill discovery and CLI/gateway command mappings through existing APIs.
7. Git: detect repository and working-tree/index state; never `git add .`; stage exact approved paths; refuse commit with unrelated staged changes or use isolated temporary index/worktree. Default auto_commit=false, auto_push=false.
8. Do not push without distinct explicit authorization. No amend or force operations.
9. Add rollback guidance and documentation for production GCP deployment.

## Tests

- Valid approval publishes correct bytes; missing/expired/wrong approval fails.
- Content changed after approval requires reapproval.
- Simultaneous publish to same destination yields exactly one winner.
- Interrupted copy does not expose incomplete skill.
- Symlink swap and collision rejected.
- Git dirty index cannot include unrelated files.
- Git failure retains truthful published/not-committed status.
- Gateway restart does not resurrect expired approval.

## Gate

Approve draft, atomically publish to temporary skills-pack Git repo, discover skill, and verify only expected paths are staged/committed when authorized. Rejection and failure cases do not alter existing skills.

## Handoff

Document approval UX, atomicity guarantees, recovery behavior, exact Git operations, test results and manual deployment procedure.
