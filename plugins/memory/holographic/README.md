# Holographic Memory Provider

Local SQLite fact store with FTS5 search, trust scoring, entity resolution, and HRR-based compositional retrieval.

## Requirements

None — uses SQLite (always available). NumPy optional for HRR algebra.

## Setup

```bash
hermes memory setup    # select "holographic"
```

Or manually:
```bash
hermes config set memory.provider holographic
```

## Config

Config in `config.yaml` under `plugins.hermes-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end (reserved for later work; off by default) |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |
| `mirror_memory_writes` | `true` | Mirror built-in `memory` tool add/replace/remove into holographic facts |
| `prefetch_min_coverage` | `0.3` | Minimum query-term coverage required for a fact to auto-recall (relevance filter) |
| `prefetch_repeat_turns` | `6` | Turns before a recalled fact can be auto-injected again in the same session |
| `auto_recall` | `true` | Automatically inject relevant stored facts before each turn, without a `fact_store` call |
| `recall_top_k` | `5` | Max facts auto-injected per turn (clamped 1-20) |
| `recall_max_tokens` | `700` | Approximate token budget for the auto-injected memory block per turn (clamped 50-4000) |
| `recall_timeout_ms` | `300` | Soft deadline for scanning recall candidates; returns what was found so far instead of blocking (clamped 10-5000) |
| `recall_debug` | `false` | Log sanitized recall diagnostics (candidate/fact counts, timing) — never fact content |

### Automatic recall

Each turn, the agent calls `prefetch(query)` once (the same call site used by
both the CLI and the gateway/Telegram runtime, via `MemoryManager.prefetch_all()`
in `run_agent.py`). The holographic provider searches stored facts, filters by
relevance (`prefetch_min_coverage`) and a security firewall (credential-like or
instruction-like content is withheld), ranks and deduplicates, and returns at
most `recall_top_k` facts within the `recall_max_tokens` budget. The result is
wrapped in a `<memory-context>` fence by `MemoryManager` and explicitly labeled
as recalled data, not new user input or system instructions — so a malicious
stored fact (e.g. "ignore previous instructions...") cannot gain authority over
the model's behavior.

No `fact_store` tool call is required for ordinary recall. `fact_store` and
`fact_feedback` remain available for explicit search, structured reasoning
(`probe`/`related`/`reason`/`contradict`), adding/editing facts, and manual
diagnostics.

**To disable automatic recall** (rollback) without losing explicit memory
tools, set `auto_recall: false` under `plugins.hermes-memory-store` in
`config.yaml`, or pass `config={"auto_recall": False}` when constructing the
provider directly. The stored facts and `fact_store`/`fact_feedback` tools
are unaffected.

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | 9 actions: add, search, probe, related, reason, contradict, update, remove, list |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |
