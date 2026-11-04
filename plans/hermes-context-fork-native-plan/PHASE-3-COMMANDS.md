# Phase 3 — Gateway and CLI Commands (P0)

## Objective
Expose `/context`, `/ctx`, `/context all` through the fork's existing command dispatch, help, and busy-state rules. 

## Implementation
1. Locate and reuse actual gateway/CLI command registry and native dispatch (do not add a parallel framework).
2. Route commands to read-only snapshot service and render appropriate platform output.
3. Support `/context`, `/ctx` aliases and `/context all`; for malformed args return concise usage.
4. Ensure command discovery (`/commands` or equivalent) lists supported commands; preserve legacy commands and existing precedence.
5. Decide explicitly whether commands are available while a turn is busy; avoid racing mutable agent state. No LLM calls.
6. Keep per-platform/chat/session isolation and bounded output. Avoid leaking provider credentials or prompt content.

## Tests
Gateway registration and aliases, CLI handling, help discovery, unknown subcommands, concurrent chats, busy turns, message length limits, no LLM call, read-only state diff.

## Deliverables and gate
Working P0 commands, help docs, integration tests. STOP for approval.
