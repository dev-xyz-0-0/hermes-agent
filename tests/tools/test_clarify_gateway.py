"""Tests for gateway clarify request lifecycle."""

import threading
import time

from tools.clarify_gateway import (
    clear_session,
    mark_awaiting_text,
    pending,
    register,
    register_gateway_notify,
    resolve,
    resolve_id,
    unregister_gateway_notify,
    wait,
)


def test_fifo_requests_are_presented_in_order():
    first = register("clarify-test-fifo", "First", None)
    second = register("clarify-test-fifo", "Second", None)
    assert pending("clarify-test-fifo") is first
    assert resolve("clarify-test-fifo", "one")
    assert wait(first, 1) == "one"
    assert pending("clarify-test-fifo") is second
    assert resolve("clarify-test-fifo", "two")
    assert wait(second, 1) == "two"


def test_unknown_or_repeated_ids_are_idempotent():
    assert not resolve_id(-1, "nope")
    req = register("clarify-test-idempotent", "Pick", ["A", "B"])
    assert resolve_id(req.request_id, "2")
    assert not resolve_id(req.request_id, "1")
    assert wait(req, 1) == "B"


def test_notify_callback_runs_only_for_new_queue_and_can_unregister():
    seen = []
    register_gateway_notify("clarify-test-notify", lambda req: seen.append(req.question))
    first = register("clarify-test-notify", "First", None)
    second = register("clarify-test-notify", "Second", None)
    assert seen == ["First"]
    unregister_gateway_notify("clarify-test-notify")
    third = register("clarify-test-notify", "Third", None)
    assert seen == ["First"]
    clear_session("clarify-test-notify")


def test_wait_and_resolve_choice_by_id():
    req = register("clarify-test-choice", "Pick", ["A", "B"])
    result = []
    worker = threading.Thread(target=lambda: result.append(wait(req, 2)))
    worker.start()
    assert resolve_id(req.request_id, "2")
    worker.join(2)
    assert result == ["B"]
    assert pending("clarify-test-choice") is None


def test_open_ended_and_other_wait_for_text():
    open_req = register("clarify-test-open", "Explain", None)
    assert open_req.awaiting_text
    assert resolve("clarify-test-open", "A free-form answer")
    assert wait(open_req, 1) == "A free-form answer"

    choice_req = register("clarify-test-other", "Pick", ["A"])
    assert not choice_req.awaiting_text
    assert mark_awaiting_text(choice_req.request_id)
    assert choice_req.awaiting_text
    assert resolve("clarify-test-other", "custom")
    assert wait(choice_req, 1) == "custom"


def test_clear_session_wakes_waiter():
    req = register("clarify-test-clear", "Question", None)
    result = []
    worker = threading.Thread(target=lambda: result.append(wait(req, 5)))
    worker.start()
    assert clear_session("clarify-test-clear") == 1
    worker.join(2)
    assert result == ["[clarify cancelled because the session ended]"]


def test_timeout_removes_pending_request():
    req = register("clarify-test-timeout", "Question", None)
    assert wait(req, 0.01) == "[user did not respond before the clarify timeout]"
    assert pending("clarify-test-timeout") is None


def test_timeout_none_waits_until_resolved():
    req = register("clarify-test-no-timeout", "Question", None)
    result = []
    worker = threading.Thread(target=lambda: result.append(wait(req, None)))
    worker.start()
    time.sleep(0.02)
    assert worker.is_alive()
    assert resolve("clarify-test-no-timeout", "answer")
    worker.join(2)
    assert result == ["answer"]
