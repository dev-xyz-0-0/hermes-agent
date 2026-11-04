# Codex Master Prompt — Fork-Native `/context`

You are working **only** in the existing checkout of `dev-xyz-0-0/hermes-agent`.

Read `README.md`, `ALGORITHMS.md`, `TEST-MATRIX.md`, and the phase document for the current phase. Follow local `AGENTS.md` and repository conventions. Use CodeGraph MCP if already configured, but do not install it as part of this work.

Upstream `https://github.com/NousResearch/hermes-agent` is a read-only design reference. Inspect raw source through HTTP/GitHub tools if available. Do not run `git fetch`, add remotes, create upstream refs, or merge/cherry-pick/rebase/reset. Do not assume previously reported upstream function names or file sizes are current. Record exact source links and revisions if obtainable.

Before changes, inspect `git status --short`; never discard preexisting modifications. At every phase: (1) inventory actual fork APIs; (2) explain proposed changes and file paths; (3) implement the minimum necessary; (4) add tests; (5) run targeted tests; (6) report evidence, diffs, risks, and next-phase readiness; (7) STOP for approval. Phase 0 is strictly read-only.

Do not change compression thresholds, gateway session semantics, memory DB schema, or production configuration without explicit approval. Diagnostics must be local/read-only, with no model calls, tool executions, secret exposure, or session writes. Do not commit, push, restart, or deploy without explicit approval.

Start with **PHASE-0-AUDIT.md only**. Return a concrete file-by-file compatibility matrix and an evidence-based Phase 1 proposal; do not proceed automatically.
