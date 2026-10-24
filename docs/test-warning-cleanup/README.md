# Test Warning Cleanup

## Status

Future maintenance work. These warnings are currently non-blocking and are not
known to be regressions from the P0 logging reliability changes.

The reference CI run completed successfully:

```text
10180 passed, 40 skipped, 139 warnings in 232.81s
```

The suite excluded `tests/integration` and `tests/e2e` and ran with pytest-xdist.

## Why Track This

A passing suite can still hide incorrect asynchronous cleanup. In particular,
`RuntimeWarning: coroutine ... was never awaited` may indicate either:

- a test configured an `AsyncMock` where synchronous behavior was expected;
- production code called an asynchronous callback without awaiting it;
- a coroutine was created before an exception or fallback abandoned it; or
- an async subprocess, task, client, or event loop was not closed cleanly.

These warnings do not currently fail CI, but they can obscure new resource
leaks and make future Python or dependency upgrades harder.

## Warning Inventory

### Priority 1: unawaited application coroutines

- `probe_mcp_server_tools.<locals>._probe_all was never awaited`
  - Observed during pytest garbage collection.
  - Inspect MCP probing error, timeout, and event-loop bridge paths.
  - Confirm every created `_probe_all()` coroutine is awaited, scheduled on an
    owned loop, or explicitly closed when scheduling fails.
- `Process.communicate was never awaited`
  - Reported while running
    `tests/cli/test_resume_display.py::TestDisplayResumedHistory::test_large_history_shows_truncation_indicator`.
  - Inspect async subprocess mocks and subprocess cleanup in that test path.

### Priority 2: unawaited `AsyncMock` calls

Warnings were reported in or around:

- `tests/acp/test_events.py`
- `tests/acp/test_mcp_e2e.py`
- `tests/hermes_cli/test_setup_model_provider.py`
- `tests/gateway/test_slack.py`
- `tests/tools/test_mcp_tool.py`

For each warning, determine whether the callable contract is synchronous or
asynchronous before changing production code. Prefer `MagicMock` for
synchronous callbacks and `AsyncMock` only when the caller must await the
result.

### Priority 3: framework and dependency warnings

- `aiohttp.NotAppKeyWarning`
  - Repeated in API-server tests.
  - Replace string application keys with typed `web.AppKey` instances where
    compatible.
- `websockets.InvalidStatusCode` and `websockets.legacy` deprecations
  - Track upstream `lark_oapi` compatibility and direct legacy imports.
- HTTPX `verify=<str>` deprecation
  - Migrate callers to an `ssl.SSLContext` when the affected authentication
    paths are next updated.
- `audioop` deprecation
  - Track Python 3.13 compatibility for voice/audio dependencies.

Most of the 139 warnings are repetitions of a few sources: API-server AppKey
warnings account for over 100 entries, and Slack tests repeat another async
warning. Treat the inventory as a set of distinct causes, not 139 separate
defects.

## Investigation Workflow

Work on one warning family at a time:

1. Reproduce the smallest named test without xdist.
2. Add `-W error::RuntimeWarning` for unawaited-coroutine work.
3. Enable tracemalloc when the allocation site is unclear.
4. Inspect whether the warning originates in production code or test mocking.
5. Fix ownership at the coroutine/task/subprocess creation boundary.
6. Verify cancellation and failure paths as well as success paths.
7. Run the affected module, then the full non-integration suite.

Example commands:

```bash
source venv/bin/activate
PYTHONTRACEMALLOC=25 python -m pytest -n 0 \
  tests/tools/test_mcp_tool.py -q \
  -W error::RuntimeWarning
```

```bash
source venv/bin/activate
python -m pytest -n 0 tests/acp/test_events.py -q \
  -W error::RuntimeWarning
```

```bash
source venv/bin/activate
python -m pytest tests/ -q \
  --ignore=tests/integration \
  --ignore=tests/e2e \
  --tb=short -n auto
```

## Implementation Guardrails

- Do not silence `RuntimeWarning` globally to make the count smaller.
- Do not blindly wrap coroutines in `asyncio.create_task()`; every task needs a
  lifecycle owner, exception reporting, and shutdown behavior.
- Preserve cancellation semantics and do not report normal cancellation as an
  application error.
- Do not change synchronous callback APIs to async solely to accommodate an
  incorrectly configured test mock.
- Restore event loops, exception handlers, environment variables, and global
  hooks modified by tests.
- Avoid network access, production credentials, live gateways, and live cron
  jobs while reproducing warnings.

## Acceptance Criteria

- [ ] `_probe_all was never awaited` no longer appears in the targeted or full
  non-integration suite.
- [ ] `Process.communicate was never awaited` is eliminated from the resume
  display tests.
- [ ] ACP, MCP, setup, and Slack tests emit no unawaited-`AsyncMock` warnings.
- [ ] New or updated tests cover success, exception, cancellation, and cleanup
  behavior for each corrected async owner.
- [ ] No warning is removed solely through a broad pytest warning filter.
- [ ] The non-integration/non-E2E suite still passes.
- [ ] Remaining dependency deprecations are documented with an owner or
  upstream migration path.

## Relationship to Logging Reliability

The background exception hooks documented in
[`../logging-and-errors.md`](../logging-and-errors.md) improve visibility of
unhandled thread, asyncio, and detached-future failures. They do not repair an
unawaited coroutine: ownership must be corrected at the creation/call site.
Future warning fixes should retain the logging hooks as a last-resort safety
net rather than relying on them for normal control flow.
