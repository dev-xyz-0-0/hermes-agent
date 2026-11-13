Diagnose and improve observability for built-in memory initialization failures.

Context

The effective config contains:

memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
  provider: holographic
  flush_min_turns: 6
  nudge_interval: 10

Despite this, some agent instances return:

{"success": false, "error": "Memory is not available. It may be disabled in config or this environment."}

Earlier instances successfully used the built-in `memory` tool, so this may involve a different profile/process, `skip_memory=True`, configuration loading failure, or `MemoryStore` initialization failure.

Relevant code

- `run_agent.py`
  - configuration loading around the `_load_agent_config()` call
  - built-in memory initialization around `_memory_store = None`
  - agent-level `memory` tool interception
- `tools/memory_tool.py`
  - `memory_tool()`
  - `MemoryStore`
- `tools/delegate_tool.py`
  - subagents created with `skip_memory=True`
- `agent/builtin_memory_provider.py`

Task

Add safe diagnostics that identify why built-in memory is unavailable without changing memory behavior, weakening security, or exposing sensitive data.

Requirements

1. Replace the silent configuration-loading exception in `AIAgent.__init__()` with a warning that:
   - states that agent configuration loading failed;
   - includes `exc_info=True`;
   - does not log configuration contents, environment variables, credentials, or memory content;
   - preserves the current fallback to an empty configuration.

2. Replace the silent built-in-memory initialization exception with a warning that:
   - states that built-in memory initialization failed and the `memory` tool will be unavailable;
   - includes `exc_info=True`;
   - reports only safe context:
     - platform;
     - session ID if existing logging policy permits it;
     - active profile or resolved `HERMES_HOME`;
     - `memory_enabled`;
     - `user_profile_enabled`;
     - `skip_memory`;
   - does not log `MEMORY.md`, `USER.md`, their contents, complete config mappings, or environment values;
   - preserves the current fail-open agent initialization behavior.

3. Add a safe initialization-status log after the memory initialization block:
   - use DEBUG for normal state, not WARNING;
   - include:
     - whether configuration loaded;
     - whether built-in memory was requested;
     - whether a store is available;
     - whether memory was intentionally skipped;
     - platform;
     - active profile or sanitized/resolved Hermes home;
   - never include memory entries or credentials.

4. Improve the unavailable-memory tool error so it distinguishes safe high-level states where possible:
   - intentionally skipped for this agent context;
   - disabled by effective configuration;
   - requested but initialization failed;
   - unavailable for an unknown reason.
   Keep the response concise and do not return exception details, filesystem internals, or secrets to the model/user.

5. Store an internal state/reason on `AIAgent`, for example:
   - `available`
   - `disabled`
   - `skipped`
   - `config_load_failed`
   - `initialization_failed`
   The exact representation is flexible, but it must be initialized deterministically and remain internal.

6. Prevent avoidable calls where appropriate:
   - inspect whether the built-in `memory` tool schema is exposed to agents created with `skip_memory=True` or when both built-in targets are disabled;
   - if it can be removed safely at tool-resolution time without changing toolsets during a conversation or breaking prompt caching, do so;
   - otherwise retain the schema and return the improved state-specific error;
   - subagents must not gain permission to write shared `MEMORY.md`/`USER.md`.

7. Preserve existing behavior:
   - Holographic remains additive and independent.
   - `provider: holographic` must not disable built-in memory.
   - Existing memory character limits and threat scanning remain unchanged.
   - Existing prompt caching behavior remains unchanged.
   - No toolset or system-prompt changes after conversation initialization.
   - No changes to memory files or production memory data.
   - No hardcoded `~/.hermes`; use `get_hermes_home()` and profile helpers.
   - Do not expose credentials or memory content in logs.

Tests

Add targeted regression tests covering:

1. Configuration loads and both built-in targets are enabled:
   - store is available;
   - internal state is `available`;
   - no warning is emitted.

2. Both targets are disabled:
   - store remains unavailable;
   - state is `disabled`;
   - no initialization-failure warning is emitted;
   - tool response explains that built-in memory is disabled.

3. `skip_memory=True`:
   - store remains unavailable;
   - state is `skipped`;
   - no initialization-failure warning is emitted;
   - subagents cannot write built-in memory.

4. Configuration loading raises:
   - agent initialization continues;
   - warning with traceback is emitted;
   - state records `config_load_failed`;
   - log output contains no config values, memory content, or credentials.

5. `MemoryStore` construction raises:
   - agent initialization continues;
   - warning with traceback is emitted;
   - state records `initialization_failed`;
   - memory tool returns the safe state-specific error.

6. `MemoryStore.load_from_disk()` raises:
   - same behavior as construction failure;
   - no partially initialized store is treated as available.

7. `provider: holographic` with built-in memory enabled:
   - both providers initialize independently;
   - built-in `memory` and Holographic `fact_store` remain distinct;
   - failure of one does not incorrectly disable the other.

8. Different profiles:
   - diagnostics report the correct active profile/path;
   - no profile reads or writes another profile’s memory.

9. Logging safety:
   - synthetic API keys, tokens, memory contents, and environment values do not appear in warning or debug logs.

10. Existing memory tests continue to pass:
    - add/replace/remove;
    - character-limit rejection;
    - threat-pattern rejection;
    - prompt snapshot stability;
    - provider lifecycle and shutdown.

Validation

Run:

source venv/bin/activate
python -m pytest tests/tools/test_memory_tool.py -q
python -m pytest tests/agent/test_memory_provider.py -q
python -m pytest tests/agent/test_memory_plugin_e2e.py -q
python -m pytest tests/agent/test_memory_input_provenance.py -q
python -m pytest tests/run_agent/test_memory_provider_release.py -q
python -m pytest tests/run_agent/test_memory_provider_shutdown.py -q
python -m pytest tests/plugins/memory/ -q

Then run the full suite:

python -m pytest tests/ -q

Also run:

git diff --check

Return

- Root cause(s) found.
- Files and functions changed.
- Explanation of the new internal states.
- Logging examples with sensitive values redacted.
- Tests added and exact results.
- Any remaining path where the `memory` schema can be exposed while unavailable.
- Confirmation that no production memory files or databases were modified.