# P1 Logging Observability, Exception Auditing, and Redaction Plan

## Summary

Implement the four P1 objectives on top of the completed P0 work, with these corrections to the submitted plan:

- Limit the silent-exception audit to logging, cron, gateway lifecycle/shared adapter infrastructure, and security-sensitive enforcement boundaries. The repository contains roughly 1,469 broad handlers, so a full-repository audit is a separate project.
- Do not create or switch branches because the current `main` worktree contains uncommitted P0 and documentation changes.
- Define gateway ownership by logger namespace plus an explicit component marker; do not infer ownership from the process or filename.
- Treat correlation as execution-scoped observability, not persisted job state. No cron schema migration.
- Keep pattern redaction as defense in depth; data minimization remains mandatory.
- Preserve the existing multiprocess rotation limitation documented in `docs/logging-and-errors.md`.

## Implementation Changes

### Cron correlation

- Add logging context support alongside `hermes_logging.py` using three `ContextVar` values: `job_id`, `run_id`, and `profile`, all defaulting to `"-"`.
- Provide a context manager that sets all three values and resets tokens in reverse order in `finally`.
- Generate one full `uuid4().hex` run ID in `tick()` for each due job, before `advance_next_run()`. Keep the context active through execution, output persistence, delivery, and final status persistence.
- Resolve the profile through `hermes_cli.profiles.get_active_profile_name()`; do not parse `HERMES_HOME` directly.
- Preserve context in the existing cron executor through its current `copy_context()` call. Verify propagation through synchronous delivery, `asyncio.run()`, and gateway-loop delivery.
- Add an idempotent handler filter that injects safe defaults and a preformatted correlation prefix. Update file formatters to include the prefix without changing non-cron output beyond an empty field.
- Emit stable lifecycle identifiers at existing ownership boundaries:
  - `cron.execution.started`
  - `cron.execution.succeeded`
  - `cron.execution.failed`
  - `cron.delivery.started`
  - `cron.delivery.succeeded`
  - `cron.delivery.failed`
- Keep current job status, scheduling, delivery, and retry semantics authoritative and unchanged.

### Bounded exception audit

- AST-scan and manually review silent or visibility-deficient handlers only in:
  - `hermes_logging.py`, `hermes_exception_hooks.py`, and `agent/redact.py`
  - `cron/`
  - `gateway/run.py`, `gateway/status.py`, and `gateway/platforms/base.py`
  - security-sensitive approval, authentication, credential-loading, pairing, and authorization paths reached from those boundaries
- Exclude already contextualized handlers and platform-specific adapter internals unless CodeGraph shows they suppress a failure crossing a reviewed boundary.
- Classify candidates as intentional fallback, recoverable operational failure, unexpected failure, security-sensitive failure, or cancellation/shutdown.
- Modify only handlers where visibility is insufficient:
  - DEBUG for harmless diagnostic fallback
  - WARNING for degraded but continuing operation
  - ERROR with traceback where the boundary converts an unexpected failure into operational state
  - safe ERROR plus existing fail-closed behavior for security enforcement
- Never log complete payloads, prompts, environment mappings, headers, credentials, or asyncio context dictionaries.
- Record all inspected candidates, retained suppressions, changes, tests, and numerical totals in `docs/exception-handling-audit.md`. Record unreviewed repository areas as a future backlog rather than claiming global completeness.

### Gateway log isolation

- Extend `_add_rotating_handler()` to return the existing or newly created handler so gateway initialization can configure the correct handler without scanning or mutating unrelated handlers.
- Add an idempotent gateway-only filter to `gateway.log`; do not filter the root logger, `agent.log`, or `errors.log`.
- Include records when either:
  - the logger is `gateway` or starts with `gateway.`, or
  - the record carries the explicit component marker `hermes_component="gateway"`.
- Mark gateway-owned records outside the namespace only where necessary, such as gateway-installed exception-hook failures. Keep ordinary `cron.*`, `agent.*`, root, third-party, and detached plugin records out of `gateway.log`.
- Gateway cron-ticker infrastructure remains included because it logs through `gateway.run`; cron execution records remain excluded.
- Preserve rotation, redacting formatters, severity, paths, and repeated initialization behavior.

### Redaction verification and hardening

- Add persisted-output tests for ordinary messages, exception messages, tracebacks, nested exception chains, cron failures, thread failures, asyncio failures, gateway-filtered failures, and representative third-party exception text.
- Cover synthetic provider tokens, bearer headers, Telegram tokens, Slack/Discord-style credentials, environment assignments, JSON secret fields, database URLs, userinfo URLs, query-string tokens, and private-key blocks.
- Extend `redact_sensitive_text()` only for demonstrated gaps, using module-level compiled patterns and avoiding broad masking of normal identifiers.
- Redact URL userinfo and recognized sensitive query parameters while preserving non-sensitive scheme, host, path, and query fields needed for diagnosis.
- Keep `RedactingFormatter` as the final persistent-output boundary and avoid serializing complete request/response objects upstream.
- Document that unknown formats and arbitrary sensitive user text cannot be guaranteed safe through pattern matching alone.

## Interfaces and Compatibility

- New internal context API: cron logging context manager plus accessors/filter fields for `job_id`, `run_id`, `profile`, and formatted correlation prefix.
- `_add_rotating_handler()` changes from returning `None` to returning the configured `RotatingFileHandler`; existing callers that ignore the result remain compatible.
- Gateway-owned records outside `gateway.*` may use `extra={"hermes_component": "gateway"}`.
- No public CLI, configuration, cron persistence, gateway protocol, retry, scheduling, or database changes.
- No branch creation, logging dependency, JSON logging, remote service, or root README change unless the repository gains a local documentation index.

## Test Plan

- Correlation:
  - Unique run IDs across executions of the same job.
  - Stable job/run/profile values through scheduling, executor execution, persistence, delivery, and failure.
  - Correct default/named/custom profile resolution.
  - Reset after success and exception.
  - No cross-job or concurrent-context leakage.
  - Valid formatting for ordinary and third-party records without cron context.
- Exception audit:
  - Targeted regression test for every modified handler.
  - Preserve fallback result, propagation, cancellation, fail-closed behavior, status persistence, and traceback ownership.
  - Assert no duplicate ERROR traceback at adjacent boundaries.
- Gateway isolation:
  - Verify the routing matrix below and repeated initialization/filter idempotency.

| Record ownership | `agent.log` | `errors.log` if WARNING+ | `gateway.log` |
|---|---:|---:|---:|
| `gateway.*` | Yes | Yes | Yes |
| Explicit gateway component | Yes | Yes | Yes |
| `agent.*` | Yes | Yes | No |
| `cron.*` execution | Yes | Yes | No |
| Root/third-party | Yes | Yes | No |

- Redaction:
  - Inspect actual formatted file output, not only helper return values.
  - Assert complete synthetic secrets are absent from every applicable log.
  - Verify nested chains and P0 background hooks retain exception type and traceback after redaction.
- Validation sequence:
  1. New correlation, gateway-filter, audit-regression, and redaction tests.
  2. Existing logging, cron, exception-hook, profile, gateway lifecycle, and adapter tests.
  3. Non-integration/non-E2E suite using the known passing CI command.
  4. `git diff --check` and final dirty-worktree review.
- Report exact pass/fail/skip/warning counts without treating existing warning cleanup as part of P1.

## Documentation and Acceptance

- Update `docs/logging-and-errors.md` with correlation fields, lifecycle events, gateway routing, severity policy, redaction coverage, grep examples, and limitations.
- Create `docs/exception-handling-audit.md` containing the bounded inventory, classifications, changes, retained suppressions, tests, totals, and unreviewed backlog.
- Update `docs/test-warning-cleanup/README.md` only if P1 changes affect its warning inventory.
- Completion requires:
  - Stable correlation through every supported `tick()` execution lifecycle with no context leakage.
  - Gateway filtering that matches the routing matrix without affecting other handlers.
  - Persisted-output redaction tests for all specified paths.
  - Every modified exception boundary tested and every reviewed silent candidate documented.
  - P0 behavior and focused regression suites passing.
- Residual risks to retain explicitly: multiprocess rotation races, failures before logging initialization, native/SIGKILL failures, unknown secret formats, and exception handlers outside the bounded audit.
