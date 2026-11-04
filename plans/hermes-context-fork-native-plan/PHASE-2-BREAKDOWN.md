# Phase 2 — Attribution and Rendering (P0)

## Objective
Adapt upstream category attribution and human-readable rendering without copying upstream agent state contracts.

## Implementation
1. Trace fork prompt assembly and actual provider-visible messages/tool schemas.
2. Attribute base system, context files/rules, skills, built-in tools, MCP tools, subagents, memory, and conversation. Only split categories with real boundaries; otherwise use `unknown` or aggregate.
3. Estimate token cost using existing fork estimators. Keep category sum and occupancy separately labeled.
4. Implement compact messaging renderer and optional 100-cell CLI grid; cap output to platform limits and escape formatting.
5. Implement expanded `all` view with per-skill/per-toolset estimates where available; avoid leaking content or paths containing secrets.
6. Ensure Holographic dynamic recall isn't counted twice; detailed integration deferred to Phase 6.

## Tests
Deterministic breakdown fixtures; mutually exclusive tool categories; empty skills; many MCP tools; long names; Unicode; zero/unknown tokens; large outputs; no content disclosure; renderer snapshots.

## Deliverables and gate
Shared breakdown data contract and renderers; focused tests; limitations table. STOP for approval.
