# Algorithm Contracts and Invariants

## A. Occupancy
Use provider-reported *prompt/input tokens for the relevant current prompt* where valid; otherwise a validated usage anchor plus appended-message estimate; otherwise a full local prompt estimate; otherwise unknown. Never equate cumulative billed usage to active context occupancy.

Represent `{used_tokens: int|null, provenance: provider_usage|anchored_estimate|local_estimate|unavailable, observed_at, session_key, model_key, confidence_or_limitations}`. A stale or mismatched measurement must not masquerade as fresh.

## B. Pressure
For context window `W > 0`, compaction budget `B > 0` derived from **existing effective fork configuration**, and usage `U >= 0`:

- `window_pct = 100 * U / W`
- `compaction_pct = 100 * U / B`
- `remaining = max(0, B-U)`

Display threshold percentage and absolute token budget. Do not cap pressure at 100%. If any input is unknown/invalid, return `unknown`, not zero. Beware integer rounding and threshold semantics (fraction vs absolute count).

## C. Attribution
Partition provider-visible prompt material into non-overlapping categories: base/system, rules/context files, skills, built-in tool definitions, MCP tool definitions, subagents, memory (static and injected), conversation, and unknown/unattributed. Prefer boundaries from the fork's prompt builder; avoid estimating the same tool schema or memory block twice. Category estimates are not necessarily additive to the authoritative provider count; show residual only if justified and label it.

## D. Usage anchor (conditional)
Anchor includes session identity, model/context configuration, provider prompt measurement, transcript prefix length, and a stable fingerprint over **provider-visible** message fields. Validate prefix integrity before adding estimated appended messages. Invalidate on `/new`, `/reset`, compaction rewrite, prefix mutation, model/context switch, or incompatible serialization. Do not persist raw prompt/message contents or secrets. Account for reasoning/tool turns without double counting. If fork already supports equivalent logic, adapt instead of duplicating.

## E. Warning ownership
Associate warning events with the active session identity/generation. At asynchronous delivery, check ownership against the active viewer/session binding. On `/new`, old queued warnings must not be presented as current-session pressure. Distinguish actual new-session prompt overhead from stale counters and delayed notifications. Avoid modifying reset behavior until a failing regression test demonstrates cause.

## F. Read-only semantics
Snapshotting must not call the LLM, invoke tools, mutate transcript, increment compaction counters, alter anchors, or create a new session. Concurrency must produce a consistent snapshot or explicitly identify stale/incomplete data.
