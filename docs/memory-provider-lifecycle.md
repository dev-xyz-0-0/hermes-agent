# Memory-Provider Lifecycle

An external memory provider (e.g. Holographic) attaches to an `AIAgent` via
`MemoryManager`. Over the agent's lifetime, the gateway creates, caches,
reuses, and eventually discards the underlying `AIAgent` object many times
for many different reasons — a `/new` command, a `/resume`, a config
change, a gateway restart. Only some of those reasons mean the
**conversation** actually ended. Conflating "the runtime object was
discarded" with "the conversation ended" either leaks provider resources
(SQLite handles, open connections) or — worse — triggers premature
extraction (`on_session_end`) on a conversation the user can still resume.

This document describes the four lifecycle classes every such path belongs
to, the two `AIAgent` methods that implement them, and where each class is
wired up in the gateway.

## The four classes

| Class | Meaning | Provider methods called |
|---|---|---|
| **A — Real boundary** | The durable conversation has actually ended. | `on_session_end(transcript)` then `shutdown_all()`, exactly once. |
| **B — Switch / fork** | The user is moving to a different durable session; the one they're leaving remains resumable. | Nothing, or `shutdown_all()` only if the runtime object is discarded. Never `on_session_end`. |
| **C — Resource release** | The runtime `AIAgent` object is being discarded, but the durable conversation continues (it will get a fresh agent next turn). | `shutdown_all()` only. Never `on_session_end`. |
| **D — Continuation** | The conversation continues uninterrupted; only its *context window* is being managed. | `on_pre_compress(messages)`. Never `on_session_end`. |

## The two `AIAgent` methods

Both live in `run_agent.py` and share the same terminal state —
`_memory_provider_finalized` (bool) guarded by
`_memory_provider_finalization_lock` — so they are mutually exclusive:
whichever one is called first on a given agent "wins" and does its work;
the other becomes a no-op. This means a real boundary and a resource
release can never both run for the same agent instance, and a given agent
is torn down at most once even if two gateway code paths race to clean it
up concurrently (e.g. the expiry watcher and the next inbound message).

```python
agent.shutdown_memory_provider(transcript)  # Class A: on_session_end + shutdown_all
agent.release_memory_provider()             # Class C: shutdown_all only
```

Neither method is called per-turn. Both are only ever called at an actual
lifecycle event, from the gateway (`gateway/run.py`) or the CLI
(`cli.py`'s process-exit cleanup, which is itself a real boundary — a CLI
session is 1:1 with the process).

## Where each class is wired up (`gateway/run.py`)

Two shared helpers do the capture → call-outside-the-lock → evict dance so
no call site duplicates the locking discipline:

- `_finalize_and_evict_cached_agent(session_key, transcript)` — Class A.
- `_release_and_evict_cached_agent(session_key, expected_agent=None)` — Class C.

Both **never run provider code while holding `_agent_cache_lock`**: they
capture the cached `(agent, signature)` tuple under the lock, release it,
call the provider method, then re-acquire the lock only to evict — and only
if the entry still holds the *exact same* agent object (an identity check),
so a concurrently-installed replacement is never evicted by mistake.

| Path | Class | Mechanism |
|---|---|---|
| `/new`, `/reset` (any platform) | A | `_handle_reset_command` → `_finalize_and_evict_cached_agent` |
| True session expiration (background watcher) | A | `_session_expiry_watcher` → `_finalize_and_evict_cached_agent`, looked up against `_agent_cache` (not `_running_agents`, which is only populated mid-turn) |
| Next-message expiration fallback | A | `_finalize_expired_session_before_rotation`, called before `get_or_create_session()` can silently rotate the session_id out from under us |
| `/resume` | B | `_handle_resume_command` → `_release_and_evict_cached_agent` before `switch_session` |
| `/branch` | B | `_handle_branch_command` → `_release_and_evict_cached_agent` before `switch_session` |
| Gateway shutdown (`stop()`) | C | Releases every agent found in `_running_agents` ∪ `_agent_cache` (deduplicated by identity) |
| Config-signature replacement | C | The turn-level agent-cache block releases the outgoing agent when a new one is constructed for a changed model/provider/toolset signature |
| Fallback-model eviction | C | The fallback-tracking block releases the agent `_evict_cached_agent` just popped |
| `/compress`, automatic compression | D | `AIAgent._compress_context` → `MemoryManager.on_pre_compress` (unchanged by this design — never calls `on_session_end`) |

## Why `/resume` and `/branch` discard the cached agent

Both evict the cached `AIAgent` for the session key being left behind
rather than leaving it in `_agent_cache`. The cached agent was constructed
with `session_id` as a constructor argument — it isn't something that gets
repointed when the durable session pointer moves. Reusing the same object
against a different durable session risks it operating with a stale
`session_id` anywhere that attribute is read later. Discarding it costs
nothing extra: the gateway already has a "no cache hit → construct fresh
agent" path that runs on every config change, so the next turn simply goes
through that same, already-exercised path.

## Tests

- `tests/run_agent/test_memory_provider_shutdown.py` /
  `test_memory_provider_release.py` — at-most-once idempotency for each
  method individually, concurrent-call safety, failure isolation, and the
  cross-method race (shutdown vs. release on the same agent).
- `tests/gateway/test_session_boundary_hooks.py` — `/new`/`/reset` across
  platforms, including Holographic namespace isolation.
- `tests/gateway/test_resume_command.py`, `test_branch_command.py` —
  Class B: eviction + release, never `on_session_end`.
- `tests/gateway/test_config_signature_release.py`,
  `test_fallback_eviction_release.py` — Class C release on agent
  replacement/eviction.
- `tests/gateway/test_session_expiry_watcher.py`,
  `test_next_message_expiration_fallback.py` — Class A for true expiration,
  including the watcher-vs-next-message race.

All of the above assert, where relevant, that `_agent_cache_lock` is never
held while provider code runs.
