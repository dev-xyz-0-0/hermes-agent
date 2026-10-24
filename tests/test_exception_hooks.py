"""Tests for lifecycle-scoped background exception reporting."""

import asyncio
import logging
import threading
from concurrent.futures import Future

import pytest

import hermes_exception_hooks as hooks
from agent.redact import RedactingFormatter


@pytest.fixture(autouse=True)
def _restore_hooks():
    original = threading.excepthook
    yield
    hooks.restore_thread_exception_hook()
    threading.excepthook = original


def test_thread_exception_logged_once_and_previous_hook_called(caplog):
    previous_calls = []
    threading.excepthook = previous_calls.append

    assert hooks.install_thread_exception_hook() is True
    assert hooks.install_thread_exception_hook() is False

    def fail():
        raise RuntimeError("synthetic thread failure")

    with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
        thread = threading.Thread(target=fail, name="p0-test-thread")
        thread.start()
        thread.join()

    matching = [r for r in caplog.records if "Unhandled exception in thread" in r.message]
    assert len(matching) == 1
    assert "p0-test-thread" in matching[0].message
    assert matching[0].exc_info[0] is RuntimeError
    assert len(previous_calls) == 1


def test_background_exception_tracebacks_are_redacted_in_persisted_output(tmp_path):
    path = tmp_path / "background-errors.log"
    handler = logging.FileHandler(path)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    hook_logger = logging.getLogger("hermes_exception_hooks")
    hook_logger.addHandler(handler)
    previous = threading.excepthook
    threading.excepthook = lambda _args: None
    loop = asyncio.new_event_loop()
    try:
        hooks.install_thread_exception_hook()

        def fail_thread():
            raise RuntimeError("SERVICE_TOKEN=thread-secret-value")

        thread = threading.Thread(target=fail_thread, name="redaction-thread")
        thread.start()
        thread.join()

        hooks.install_asyncio_exception_handler(loop)
        try:
            raise ValueError("https://example.test/?access_token=async-secret-value")
        except ValueError as exc:
            loop.call_exception_handler(
                {"message": "synthetic detached task failure", "exception": exc}
            )

        handler.flush()
        content = path.read_text()
        assert "RuntimeError" in content
        assert "ValueError" in content
        assert "thread-secret-value" not in content
        assert "async-secret-value" not in content
    finally:
        hooks.restore_asyncio_exception_handler(loop)
        loop.close()
        hooks.restore_thread_exception_hook()
        threading.excepthook = previous
        hook_logger.removeHandler(handler)
        handler.close()


def test_thread_system_exit_is_not_logged(caplog):
    previous_calls = []
    threading.excepthook = previous_calls.append
    hooks.install_thread_exception_hook()

    with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
        thread = threading.Thread(target=lambda: raise_system_exit(), name="normal-exit")
        thread.start()
        thread.join()

    assert not [r for r in caplog.records if "Unhandled exception in thread" in r.message]
    assert len(previous_calls) == 1


def raise_system_exit():
    raise SystemExit(0)


def test_asyncio_handler_logs_task_and_callback_failures_but_not_cancellation(caplog):
    loop = asyncio.new_event_loop()
    try:
        assert hooks.install_asyncio_exception_handler(loop) is True
        assert hooks.install_asyncio_exception_handler(loop) is False

        async def named_task():
            return None

        task = loop.create_task(named_task(), name="detached-p0-task")
        loop.run_until_complete(task)

        try:
            raise ValueError("synthetic async failure")
        except ValueError as exc:
            with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
                loop.call_exception_handler(
                    {"message": "Task exception was never retrieved", "exception": exc, "task": task}
                )

        def bad_callback():
            raise LookupError("synthetic callback failure")

        with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
            loop.call_soon(bad_callback)
            loop.run_until_complete(asyncio.sleep(0))
            loop.call_exception_handler(
                {"message": "normal cancellation", "exception": asyncio.CancelledError()}
            )

        records = [r for r in caplog.records if "Unhandled asyncio exception" in r.message]
        assert len(records) == 2
        assert any("detached-p0-task" in r.message and r.exc_info[0] is ValueError for r in records)
        assert any(r.exc_info[0] is LookupError for r in records)
    finally:
        hooks.restore_asyncio_exception_handler(loop)
        loop.close()


def test_asyncio_handler_chains_and_restores_existing_handler(caplog):
    loop = asyncio.new_event_loop()
    calls = []

    def previous(active_loop, context):
        calls.append((active_loop, context["message"]))

    loop.set_exception_handler(previous)
    try:
        hooks.install_asyncio_exception_handler(loop)
        with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
            loop.call_exception_handler({"message": "chained handler test"})
        assert calls == [(loop, "chained handler test")]
        assert hooks.restore_asyncio_exception_handler(loop) is True
        assert loop.get_exception_handler() is previous
    finally:
        hooks.restore_asyncio_exception_handler(loop)
        loop.close()


def test_observe_future_reports_failure_once(caplog):
    future = Future()
    hooks.observe_future(future, component="test-component")

    with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
        future.set_exception(RuntimeError("detached executor failure"))

    records = [r for r in caplog.records if "Detached future failed" in r.message]
    assert len(records) == 1
    assert "test-component" in records[0].message
    assert records[0].exc_info[0] is RuntimeError


@pytest.mark.parametrize("outcome", ["success", "cancelled"])
def test_observe_future_ignores_non_failures(caplog, outcome):
    future = Future()
    hooks.observe_future(future, component="test-component")
    with caplog.at_level(logging.ERROR, logger="hermes_exception_hooks"):
        if outcome == "success":
            future.set_result("ok")
        else:
            future.cancel()
    assert not [r for r in caplog.records if "Detached future failed" in r.message]
