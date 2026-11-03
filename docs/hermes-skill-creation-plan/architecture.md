# Architecture and contracts

## Flow

Task completion / explicit request -> candidate detector (optional) -> draft creation -> validation -> approval -> atomic publication -> discovery reload -> optional isolated Git staging/commit.

## Proposed integration points (verify in checkout)

- `tools/skill_manager_tool.py`: creation and management actions.
- `agent/skill_utils.py`: discovery helpers.
- `tools/skills_tool.py`: listing/viewing.
- `agent/skill_commands.py`: reload/commands.
- `tools/skills_guard.py`: security scanning.
- `run_agent.py` and `gateway/`: lifecycle and approvals.
- `plugins/memory/holographic/`: optional semantic evidence.

## Invariants

1. `external_dirs` is discovery configuration, not write authorization.
2. `create_dir` controls new skill creation; fallback to legacy home skills.
3. Drafts are never discoverable as published skills.
4. Approval is bound to draft ID, destination and immutable content hash,
   AND requires independent human authority -- a tool call's own arguments
   never constitute approval. `approve_draft()` blocks on
   `tools.approval.request_explicit_approval()` (real CLI `input()` or the
   gateway's `/approve` command); with no interactive channel available it
   fails closed. (Phase 3.1.)
5. Publication is fail-closed for security scanner errors.
6. Publication is atomic for new directories and rejects collisions.
7. Git operations never stage or commit unrelated changes.
8. Memory is advisory, not a source of execution or publication authority.
9. Profile/chat isolation is preserved.
10. Disabling new features restores legacy behavior.

## Suggested new modules (adapt to repo conventions)

`agent/skill_paths.py`; `agent/skill_creation/{config,models,staging,validation,publisher,git_integration,candidates,memory_adapter}.py`.

## State machine

`draft -> validating -> validated -> awaiting_approval -> published`; rejection, cancellation and failure may terminate applicable states. Persist state transitions, including partial Git outcomes, with durable IDs and timestamps.

## Security

Canonical path checks and symlink protection; no traversal; no automatic execution of generated code; protect secrets; no unauthenticated gateway approvals; validate content immediately before publish; concurrency lock on destination; no recursive overwrite of existing skills.
skill_paths