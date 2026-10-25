# Hermes Agent — Logging, Error Handling and Cron Diagnostics

## Overview

Hermes Agent uses Python's standard `logging` framework to capture agent activity, gateway events, and cron execution errors.

The main logging entry point is `hermes_logging.setup_logging()`.

The default log directory is:

```text
~/.hermes/logs/
```

The actual location is resolved through `get_hermes_home()`, which supports profile-aware configurations.

Logging is initialized at process startup rather than during module import:

| Process or entry point | Initialization |
|---|---|
| `hermes` CLI, including `hermes cron tick` | `setup_logging(mode="cli")` in `hermes_cli/main.py` |
| Messaging gateway | `setup_logging(mode="gateway")` in `gateway/run.py` |
| `python -m cron.scheduler` | `setup_logging(mode="cron")` in `cron/scheduler.py` |
| Code importing `cron.scheduler.tick()` | The embedding process owns logging setup |

`setup_logging()` is idempotent. Hermes marks the handlers it owns and repeated
calls do not attach another managed handler for the same purpose. If a managed
handler is removed externally, a later setup call repairs it.

## Log Files

| File | Purpose | Default level | Rotation |
|---|---|---|---|
| `agent.log` | Main application activity | INFO+ | 5 MB, 3 backups |
| `errors.log` | Warnings and errors | WARNING+ | 2 MB, 2 backups |
| `gateway.log` | Gateway-process logging | INFO+ | 5 MB, 3 backups |

The handlers overlap by severity and ownership. A gateway ERROR normally
appears in `agent.log`, `errors.log`, and `gateway.log`. The gateway handler is
attached to the root logger but accepts only `gateway`/`gateway.*` records and
records explicitly marked as gateway-owned. Ordinary agent, cron, root, and
third-party records do not enter `gateway.log`.

Cron records created during one execution carry a prefix like:

```text
[job_id=daily-report run_id=9ad... profile=default]
```

Outside a cron execution the prefix is empty, so existing non-cron log output
does not gain placeholder fields. The fields are execution-scoped and are not
persisted as cron schema state.

Each handler uses `RedactingFormatter`, including traceback formatting.

## Configuration

In `~/.hermes/config.yaml`:

```yaml
logging:
  level: INFO
  max_size_mb: 5
  backup_count: 3
```

These settings configure the main agent log. The error log has separate
hardcoded rotation settings. Cron mode does not create a separate `cron.log`.

Configuration is resolved independently for each setting:

```text
explicit setup_logging() argument > active profile config.yaml > built-in default
```

`level` accepts `DEBUG`, `INFO`, `WARN`/`WARNING`, `ERROR`, and
`FATAL`/`CRITICAL`. `max_size_mb` must be a positive integer and
`backup_count` must be a nonnegative integer; booleans are not accepted as
integers. Invalid explicit arguments raise before handlers are modified.
Invalid profile configuration fields produce a value-free stderr warning and
fall back to that field's default. An explicit `hermes_home` reads
`config.yaml` from that same home rather than from another active profile.

`backup_count: 0` uses the standard-library behavior: size-based rollover is
disabled and the active `agent.log` can grow past `max_size_mb`. Use at least
one backup when bounded disk use is required. This setting does not alter the
independent fixed rotation policies for `errors.log` or `gateway.log`.

### Reconfiguration

Ordinary repeated setup preserves existing managed handlers. Calling
`setup_logging(force=True)` reapplies the agent level and rotation properties
in place when the profile path is unchanged. When the target profile changes,
Hermes opens both replacement file handlers before swapping them in. Creation
or attachment failure removes and closes every prepared replacement while the
previous handlers remain attached and usable. Only after every replacement is
attached are the previous managed handlers detached, flushed, and closed.
Unrelated and verbose-console handlers are preserved, and root severity is
never raised in a way that would disable their diagnostic records.

In-place reconfiguration retains the existing P1 context-filter instances.
Replacement `agent.log` and `errors.log` handlers receive exactly one context
filter, while replacement gateway handlers carry forward both their context
and gateway-ownership filters.

Handlers created by older Hermes versions did not carry ownership metadata.
Hermes adopts one only when it targets the canonical `agent.log` or
`errors.log` path and matches that log's historical rotation shape. An
ambiguous same-path handler remains untouched; Hermes creates a separately
owned handler instead of changing or closing the ambiguous handler.

Editing `config.yaml` does not hot-reload a running process. Application code
must explicitly call `setup_logging(force=True)`. A separately running gateway
normally requires restart to read configuration changes. Gateway startup owns
and reconfigures `gateway.log` independently while retaining its P1 ownership
and correlation filters.

## Viewing Logs

Inspect the latest application events:

```bash
tail -n 100 ~/.hermes/logs/agent.log
```

Watch errors continuously:

```bash
tail -F ~/.hermes/logs/errors.log
```

Watch gateway activity:

```bash
tail -F ~/.hermes/logs/gateway.log
```

Inspect current disk use:

```bash
du -h ~/.hermes/logs/*
```

Search for cron failures and tracebacks:

```bash
grep -Ei 'cron|traceback|error|exception' \
  ~/.hermes/logs/errors.log
```

Search rotated logs:

```bash
grep -Ei 'ERROR|CRITICAL|Traceback' \
  ~/.hermes/logs/*.log*
```

This search covers plain-text rotated files generated by the current handler.

## Cron Job Logging

Cron jobs are managed through `cron/scheduler.py`.

The gateway launches a background cron ticker, and the scheduler uses a lock to prevent concurrent ticks.

The normal lifecycle is:

1. Identify due jobs.
2. Advance the next execution time for recurring jobs.
3. Execute the job.
4. Save the execution output.
5. Attempt delivery to the configured destination.
6. Record execution and delivery status.

Each due-job attempt receives a new UUID run ID. Its job ID, run ID, and active
profile remain bound through schedule advancement, execution, saved output,
delivery, and status persistence, then reset even when processing raises. The
stable event names are `cron.execution.started`, `cron.execution.succeeded`,
`cron.execution.failed`, `cron.delivery.started`,
`cron.delivery.succeeded`, and `cron.delivery.failed`.

Quiet agents and non-verbose CLI usage reports intentionally leave the
process-global `cron` logger namespace enabled. Raising that namespace to ERROR
would suppress INFO/WARNING lifecycle records before file handlers, correlation
filters, and redaction can process them. A gateway that was started with an
older affected version must be restarted normally after upgrading; logging
reconfiguration alone does not reset named logger levels.

Cron execution exceptions are logged using `logger.exception()` inside `run_job()`.

Unexpected failures at the per-job scheduler boundary also use
`logger.exception()`, so processing failures retain their Python traceback and
safe job ID. The gateway cron ticker applies the same policy to an exception
that escapes an entire tick while continuing with its next scheduled tick.

`hermes cron tick` inherits the CLI logging setup. Direct standalone execution
with `python -m cron.scheduler` initializes `setup_logging(mode="cron")` before
it checks or executes jobs. Library imports and direct calls to `tick()` do not
initialize logging as an import side effect.

Cron data-collection and `no_agent` scripts are user-owned child processes.
Hermes passes the resolved `HERMES_HOME`, applies a timeout, and captures a
bounded process result using the existing subprocess contract. A nonzero exit,
timeout, or spawn failure becomes a failed job result; stderr from a successful
script is not treated as a failure or copied blindly into `errors.log`.

Execution and delivery remain separate outcomes. A successful job can have a
delivery error without being reclassified as an execution failure.

### Exception severity policy

- Expected fallback or optional integration failure: DEBUG, or WARNING when an
  operator can act on it.
- Recoverable operational failure: WARNING.
- Unexpected job execution or scheduler-processing failure: ERROR with a
  traceback.
- Fatal process failure: existing fatal reporting and termination behavior.

Handled exceptions are not automatically errors. Logging every fallback at
ERROR would obscure failures that require action and produce duplicate
tracebacks at multiple call-stack levels.

## Background Exceptions

The gateway installs last-resort exception reporting only after persistent
logging is configured, and restores the previous hooks when the gateway
lifecycle ends.

- Uncaught exceptions from ordinary Python threads are written with the thread
  name and traceback, then passed to the previously installed
  `threading.excepthook` so normal Python visibility is retained. `SystemExit`
  is not treated as an application error.
- The gateway's running asyncio loop records unretrieved task exceptions and
  callback failures using selected safe metadata. Existing custom loop
  handlers are chained. Normal task cancellation is not logged as an error.
- Executor futures are not monkeypatched globally. Only futures that are
  intentionally detached are observed; currently this includes asynchronous
  holographic-memory capture. Futures awaited or consumed by their owners keep
  their existing propagation behavior.

Exception messages and formatted traceback text pass through
`RedactingFormatter`. Logs should still avoid prompts, model output, raw
environment values, and complete asyncio context dictionaries.

The formatter recognizes common provider credentials, authorization headers,
secret-like environment assignments and JSON fields, database and HTTP URL
userinfo, sensitive query parameters, Telegram/Discord-style tokens,
Slack/Discord webhook URLs, and private-key blocks. It preserves non-sensitive
URL components where practical. Pattern matching is defense in depth: unknown
credential formats and arbitrary sensitive user text cannot be guaranteed safe,
so call sites must still minimize logged data.

### Important limitations

- Expected fallback, cleanup, and handled operational exceptions may be logged
  at DEBUG or WARNING rather than ERROR.
- A failed job and a failed delivery are separate conditions.
- Process crashes may prevent final job status updates.
- No Python logging hook can report SIGKILL, hardware failure, native crashes,
  failures before logging initialization, or failures that bypass Python.
- External processes are observable only through the stdout, stderr, exit code,
  timeout, and result data that Hermes explicitly captures.
- Graceful interpreter shutdown flushes normal file records. Abrupt termination
  can lose buffered records.
- `RotatingFileHandler` is not multiprocess-safe. Concurrent low-volume appends
  are supported, but simultaneous rotation by gateway and standalone cron
  processes can race and may lose or misplace records. A centralized logging
  process or multiprocess-safe handler would be required to eliminate this
  residual risk.
- Configuration files are read at setup time; there is no automatic hot reload.
- If an unrelated or ambiguous handler already writes to a canonical Hermes
  path, it is preserved rather than adopted. Both handlers may then write that
  file; remove the external handler at its owner if duplicate records appear.
- Exception handlers outside the bounded P1 audit may still have insufficient
  visibility. See `docs/exception-handling-audit.md` for scope and backlog.

## Troubleshooting

### A cron job failed but errors.log is empty

For direct execution, use `python -m cron.scheduler`; this initializes
standalone cron logging. Code embedding `tick()` as a library call owns its
process-level logging setup.

Also inspect the saved cron output and job status. A script error or expected
operational failure may have been converted into a result rather than raised as
a Python exception.

Confirm the active profile before inspecting files:

```bash
echo "${HERMES_HOME:-$HOME/.hermes}"
```

### Gateway log is missing

Check that the gateway reached the logging initialization stage.

Some initialization failures occur before logging handlers are attached.

### Errors appear in multiple files

This is expected. The files use overlapping handlers attached to the root
logger. It does not mean the exception was raised more than once.

### Log files are not rotating

Check file permissions, actual file sizes, and whether another process has the
files open. Avoid deliberately forcing simultaneous rotation from gateway and
standalone cron processes; standard `RotatingFileHandler` does not coordinate
rotation across processes.

### Logs are missing after changing profiles

Verify the active `HERMES_HOME` and resolved log directory.

### Cron jobs run but lifecycle events disappear

Check the effective levels of the `cron` and `cron.scheduler` loggers. They
must permit INFO for start/success events and WARNING for expected failures.
If the gateway was running before the lifecycle-visibility fix was deployed,
restart it through the normal service workflow so the process-global logger
state is recreated.

## Automated Coverage

The logging reliability tests cover:

- INFO and WARNING routing, idempotent setup, and traceback redaction.
- Cron correlation uniqueness, lifecycle stability, context propagation, and
  reset behavior.
- Gateway ownership filtering and repeated filter initialization.
- Persisted redaction for messages, URL credentials, query tokens, webhook
  credentials, private keys, and nested exception chains.
- Configuration precedence, validation, zero-backup behavior, forced
  reconfiguration, creation/attachment rollback, and concurrent setup.
- Recognizable legacy-handler adoption, ambiguous-handler preservation, and
  P1 filter retention across in-place updates and replacements.
- Deterministic rollover, backup retention, and continued formatted logging.
- Profile-aware standalone scheduler logging and graceful process exit.
- Lifecycle visibility across quiet agent construction, non-verbose CLI usage
  reports, and forced logging reconfiguration.
- Low-volume concurrent appends from two processes without forcing rotation.
- Scheduler tracebacks and preservation of job failure-status updates.
- Gateway cron-ticker tracebacks.
- Thread-hook installation, chaining, restoration, and `SystemExit` behavior.
- Detached asyncio task failures, callback failures, handler chaining, and
  cancellation behavior.
- Detached executor-future failures, successful futures, and cancelled futures.
- Gateway hook installation and restoration across its lifecycle.

Run the focused suite from the repository root:

```bash
source venv/bin/activate
python -m pytest \
  -n 0 \
  tests/test_logging_context.py \
  tests/test_gateway_log_filter.py \
  tests/test_hermes_logging.py \
  tests/test_exception_hooks.py \
  tests/cron/test_logging_reliability.py \
  tests/gateway/test_exception_hook_lifecycle.py -q
```

## Remaining Work

The focused P0–P2 logging reliability implementation is complete, but the
following work remains for future reliability improvements.

### Actionable technical debt

- **Multiprocess-safe rotation:** Gateway and standalone cron processes can
  append low-volume records concurrently, but simultaneous rotation by
  `RotatingFileHandler` can race. A future design should evaluate centralized
  parent-process forwarding or a multiprocess-safe handler without changing
  the current file names and configuration contract.
- **Cron test isolation:** Some existing scheduler tests can resolve the cron
  lock path before the test fixture applies an isolated `HERMES_HOME`. They may
  collide with an active gateway lock. Move lock-path resolution behind a
  profile-aware runtime helper or patch it consistently in those tests.
- **Repository-wide regression baseline:** The focused logging, gateway, cron,
  and detached-future tests pass, but the full repository suite currently has
  unrelated failures and timeout/hanging cases. Establish a clean baseline
  before treating a full-suite result as a release gate for this work.
- **Future detached executors:** `observe_future()` is intentionally opt-in.
  Any new fire-and-forget `ThreadPoolExecutor` call site must attach the
  observer or provide its own completion/error owner.
- **Earlier startup diagnostics:** Failures before `setup_logging()` cannot
  reach Hermes file handlers. Future entry-point work may add a minimal stderr
  fallback, while avoiding logging initialization during module import.
- **Broader exception audit:** P1 reviewed the logging, cron, gateway lifecycle,
  shared-adapter, authentication, and approval boundaries only. Platform
  adapters, providers, tools, and agent internals remain a separately scoped
  backlog; see `docs/exception-handling-audit.md`.

### Inherent limitations

These are boundaries rather than implementation defects:

- SIGKILL, power or hardware failure, native crashes, and failures that bypass
  Python cannot be guaranteed to produce a log record.
- An external process that terminates without captured stdout, stderr, or a
  useful exit status cannot provide a Python traceback.
- Abrupt process termination can lose buffered records or prevent the final
  cron status update.

## Agent-Assisted Operational Validation

Use an isolated Hermes profile and give your agent this prompt:

```text
Perform a read-only operational validation of Hermes logging.

Safety constraints:

- Do not modify source code, configuration, credentials, databases, or scheduled jobs.
- Do not restart or stop the gateway.
- Do not execute live cron jobs or send gateway messages.
- Do not access another profile.
- Do not print or copy secret values.
- Do not force log rotation or generate excessive log volume.
- Use the currently active HERMES_HOME/profile.
- If a check requires mutation, synthetic exception injection, reconfiguration,
  or service restart, mark it NOT RUN and identify the relevant automated test.

Tasks:

1. Resolve and report the active profile and log directory without exposing
   credentials.
2. Check whether these files exist and are readable:
   - logs/agent.log
   - logs/errors.log
   - logs/gateway.log
3. Report file sizes, modification times, permissions, and available disk space.
4. Inspect recent and rotated log files for:
   - ERROR, CRITICAL, Traceback, and exception records
   - cron.execution.* and cron.delivery.* lifecycle events
   - job_id, run_id, and profile correlation fields
   - background thread, asyncio, and detached-future failures
5. Verify from recent records that:
   - gateway-owned records can appear in gateway.log
   - ordinary agent and cron records do not appear in gateway.log
   - WARNING+ records overlap into errors.log where expected
6. Inspect config.yaml without displaying unrelated fields or values. Report only:
   - logging.level
   - logging.max_size_mb
   - logging.backup_count
   Validate their types and ranges.
7. If backup_count is 0, explain that size-based rollover is disabled and the
   active agent.log may exceed max_size_mb.
8. Look for evidence of duplicate identical records caused by duplicate handlers.
   Do not claim handler-count verification from file inspection alone.
9. Search for obvious unredacted synthetic/test credential patterns, but never
   reproduce suspected secret text. Report only filename, line number, pattern
   category, and a masked fingerprint.
10. Run only these non-networked automated tests if the repository and virtual
    environment are available:

    source venv/bin/activate
    python -m pytest -n 0 \
      tests/test_hermes_logging.py \
      tests/test_gateway_log_filter.py \
      tests/test_logging_context.py \
      tests/test_exception_hooks.py \
      tests/cron/test_logging_reliability.py \
      tests/gateway/test_exception_hook_lifecycle.py -q

11. Run git diff --check. Do not edit files.

Return a report with:

- Overall verdict: PASS, PARTIAL, or FAIL
- Active profile and resolved log directory
- Read-only operational findings
- Automated test command and exact pass/fail/skip counts
- Checklist table with PASS, FAIL, NOT RUN, or NOT APPLICABLE
- Any suspected redaction issue with secrets masked
- Remaining risks and recommended next actions

Do not mark checks PASS without direct evidence. Clearly distinguish automated
test evidence from observations of the running instance.
```

This prompt intentionally leaves reconfiguration rollback, concurrency, synthetic failure injection, and forced rotation to automated tests rather than experimenting on your running instance.


## Operational and Automated Validation Checklist

Use an isolated profile or test installation; do not run live jobs or alter a
production gateway solely to complete this checklist.

Items involving failure injection, forced reconfiguration, concurrency, or
synthetic exceptions should be verified through the automated tests above, not
against a production gateway.

### Logging and configuration

- [ ] An INFO record appears in the active profile's `agent.log`.
- [ ] A WARNING record appears in both `agent.log` and `errors.log`.
- [ ] The active profile reads logging settings from its own `config.yaml`.
- [ ] Explicit `setup_logging()` arguments override profile configuration.
- [ ] Invalid explicit logging settings fail without changing existing handlers.
- [ ] Invalid YAML logging fields emit a safe warning and use documented defaults.
- [ ] `backup_count: 0` creates no rotated backups; the active log may exceed
  `max_size_mb`.
- [ ] `setup_logging(force=True)` updates level and rotation settings without
  duplicating managed handlers.
- [ ] Reconfiguration to another profile moves managed file handlers to the new
  profile and closes the previous handlers.
- [ ] A simulated replacement creation or attachment failure leaves the previous
  managed handlers attached and usable.
- [ ] Repeated or concurrent initialization leaves exactly one managed
  `agent.log` handler and one managed `errors.log` handler.
- [ ] Unrelated and verbose-console handlers survive forced reconfiguration.
- [ ] Recognizable legacy Hermes handlers are adopted.
- [ ] Ambiguous same-path handlers remain unchanged and are not adopted.
- [ ] Rotation retains the configured number of backups and logging continues
  after rollover.

### Gateway and routing

- [ ] Gateway startup creates and writes to `gateway.log`.
- [ ] Gateway INFO and ERROR records appear in `gateway.log`.
- [ ] Agent, cron, root, and unmarked third-party records do not enter
  `gateway.log`.
- [ ] Gateway WARNING and ERROR records continue to appear in `errors.log`.
- [ ] In-place gateway reconfiguration preserves its context and ownership
  filter instances.
- [ ] Gateway handler replacement retains exactly one context filter and one
  gateway-ownership filter.
- [ ] Repeated gateway initialization does not duplicate handlers or records.

### Cron and background failures

- [ ] A gateway cron-ticker exception appears in `errors.log` with a traceback.
- [ ] A standalone `python -m cron.scheduler` run writes to the active profile's
  log directory.
- [ ] An unexpected cron execution or processing failure includes its exception
  type, traceback, and safe job ID.
- [ ] Two attempts of the same cron job have different run IDs, stable profile
  context throughout each attempt, and no context leakage afterward.
- [ ] Cron execution and delivery failures remain distinguishable in job state.
- [ ] An uncaught named-thread exception is persisted and retains normal Python
  exception visibility.
- [ ] An unretrieved asyncio task or callback exception is persisted, while
  normal cancellation produces no ERROR record.
- [ ] An intentionally detached executor future reports an unexpected failure
  exactly once.

### Security and operational behavior

- [ ] Synthetic secrets are redacted from ordinary messages and formatted
  tracebacks in `agent.log`, `errors.log`, and `gateway.log`.
- [ ] Graceful process shutdown flushes expected records.
- [ ] Configuration-file changes are not described as automatically hot-reloaded.
- [ ] Rotation works in a single process; multiprocess rotation remains tracked
  under **Remaining Work**.
