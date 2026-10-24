# Bounded Exception-Handling Audit

## Scope and method

This P1 audit is intentionally bounded. It covers the logging implementation,
cron scheduler, gateway lifecycle, shared platform-adapter state transitions,
gateway status persistence, authentication fallbacks, and dangerous-command
approval boundary. Platform-specific adapters and the rest of the agent,
provider, and tool implementations are not certified by this audit.

The review combined an AST inventory of broad `Exception`/`BaseException`
handlers with manual inspection of boundary behavior. At the reviewed revision,
11 Python files contained 248 broad handlers, including 82 whose body was only
`pass`. These numbers are an inventory, not an assertion that every broad catch
is defective.

| File or area | Broad handlers | Pass-only | Review result |
|---|---:|---:|---|
| `hermes_logging.py` | 1 | 1 | Intentional config fallback before handlers exist |
| `hermes_exception_hooks.py` | 6 | 2 | Intentional recursion protection and safe hook chaining |
| `cron/scheduler.py` | 24 | 5 | Ownership retained; scheduler and delivery boundaries have tracebacks |
| `cron/jobs.py` | 5 | 1 | Expected parsing/storage fallback retained |
| `gateway/run.py` | 164 | 67 | Lifecycle suppressions classified; selected startup/shutdown visibility added |
| `gateway/status.py` | 2 | 0 | PID cleanup now emits DEBUG diagnostics |
| `gateway/platforms/base.py` | 15 | 3 | Runtime-status write failures are now visible |
| `tools/approval.py` | 8 | 0 | Callback failure now logs safely and remains fail-closed |
| `hermes_cli/auth.py` | 23 | 3 | Credential-source fallbacks retained; no credential values logged |

## Changes made

- Scheduler lifecycle failures use correlated, stable event names and preserve
  traceback ownership at the scheduler or delivery boundary.
- Failure to persist connected/disconnected adapter status emits DEBUG with a
  traceback. Failure to persist fatal state emits WARNING; the potentially
  sensitive fatal message is omitted.
- Dangerous-command approval callback failures emit an ERROR traceback without
  logging the command or description, then preserve the existing `deny` result.
- Stale-lock cleanup, bundled-skill synchronization, PID-file cleanup, and MCP
  shutdown failures now have DEBUG diagnostics while preserving best-effort
  behavior.
- Exception-hook records installed by the gateway are explicitly marked as
  gateway-owned so they route to `gateway.log` without admitting unrelated
  root records.

## Retained suppressions

The remaining pass-only handlers in scope were retained where logging could be
unsafe, recursive, noisy, or unavailable:

- Logging configuration reads before persistent handlers are initialized.
- Failures inside last-resort exception reporting, where another logging call
  could recurse or replace the original exception.
- Optional metadata parsing and compatibility fallbacks that return documented
  defaults.
- Cleanup paths where an adjacent owner already records the operational result.
- Credential-source probing where absence or malformed optional input is an
  expected fallback and values must not be exposed.
- Cancellation and shutdown paths that preserve normal asyncio/thread
  semantics.

These retained handlers should not be converted mechanically to ERROR logs.
Future changes should evaluate ownership, severity, duplication, and sensitive
data at each boundary.

## Regression coverage

- `tests/test_logging_context.py`: context defaults, reset, copied executor
  propagation, unique run IDs, and stable lifecycle values.
- `tests/test_gateway_log_filter.py`: gateway routing matrix, handler reuse, and
  filter idempotency.
- `tests/test_hermes_logging.py`: persisted message and nested-traceback
  redaction.
- `tests/cron/test_logging_reliability.py`: correlated scheduler traceback and
  status preservation.
- `tests/gateway/test_platform_base.py`: runtime-status failures remain visible
  without changing adapter state.
- `tests/tools/test_approval.py`: approval failure is visible, data-minimized,
  and fail-closed.
- Existing exception-hook and gateway lifecycle tests continue to cover thread,
  asyncio, executor, installation, and restoration semantics.

## Unreviewed backlog

A repository-wide search contains many additional broad handlers outside this
scope. A future audit should review platform adapters, tool implementations,
provider clients, agent internals, environment backends, and optional plugins
in owner-sized batches. Each batch should add targeted tests before changing
severity or propagation. This document must not be used to claim global
exception coverage.
