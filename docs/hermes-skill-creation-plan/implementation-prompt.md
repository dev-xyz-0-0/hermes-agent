# implementation prompt

Implement the next phase from this Hermes Autonomous Skill Creation plan in https://github.com/dev-xyz-0-0/hermes-agent.

**Phase to implement:** Phase 1 (`phase-01-create-dir.md`) unless the operator explicitly selects a later phase and supplies proof prior gates passed.

1. Read `AGENTS.md`, `README.md`, `architecture.md`, the selected phase file and `acceptance-checklist.md`.
2. Inspect the actual checkout and locate relevant source/tests; do not assume proposed filenames exist. Use CodeGraph MCP if configured.
3. Run `git status --short` before edits. Never discard unrelated work.
4. Write a short design and test plan before implementation. Prefer small, backwards-compatible changes and existing APIs.
5. Implement **only the selected phase**; no unrelated refactors, installs, service restarts, production data edits, commits or pushes without approval.
6. Add focused tests, run them, then run relevant existing regressions and lint. Use temp homes/repos; never destructive-test production paths.
7. Inspect the diff and document all behavior changes and security implications.
8. Produce a phase handoff report: changed files, tests with actual results, manual verification, risks, and whether acceptance gate passed.
9. Stop and request review before advancing to the next phase.

**Core rules:** external_dirs is discovery-only; create_dir defaults to legacy; drafts must not be discoverable; generated scripts must not run without authorization; validation fails closed; publication and Git operations require explicit scoped approval; memory is never authority.
