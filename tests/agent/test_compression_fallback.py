"""Deterministic Phase 5.5 compression fallback and ownership tests."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

from agent.auxiliary_client import (
    AuxiliaryRequestProgress,
    _call_with_progress_stream,
    consumed_summary_route,
    pin_summary_route,
    take_pinned_summary_route,
)
from agent.context_compressor import ContextCompressor
from run_agent import _CompressionControl


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        return ContextCompressor(
            model="primary-model", provider="primary", quiet_mode=True,
            protect_first_n=2, protect_last_n=2,
        )


def _messages():
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
        for index in range(14)
    ]


def _response(text="summary"):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=text),
    )])


def _available_route(*_args, **_kwargs):
    return SimpleNamespace(base_url="https://example.invalid/v1"), "fallback-model"


def test_primary_stall_uses_one_fallback_and_commits_once():
    compressor = _compressor()
    messages = _messages()
    primary_released = threading.Event()
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        progress = kwargs["progress"]
        if len(calls) == 1:
            progress.set_supported(True)
            progress.install_cancel(primary_released.set)
            assert primary_released.wait(timeout=2)
            raise InterruptedError("abandoned primary")
        progress.set_supported(False)
        return _response("fallback summary")

    with patch("agent.context_compressor.call_llm", side_effect=call), \
         patch("agent.context_compressor.resolve_task_route", return_value=("primary", "primary-model")), \
         patch("agent.context_compressor.resolve_provider_client", side_effect=_available_route):
        result = compressor.compress(
            messages,
            fallback_routes=[{"provider": "backup", "model": "fallback-model"}],
            stall_timeout=0.01,
        )

    assert result is not messages
    assert len(calls) == 2
    assert compressor.logical_attempt_count == 2
    assert compressor.fallback_attempted is True
    assert compressor.last_fallback_outcome == "succeeded"
    assert compressor.compression_count == 1


def test_stall_without_route_preserves_exact_transcript():
    compressor = _compressor()
    messages = _messages()
    released = threading.Event()

    def stalled(**kwargs):
        progress = kwargs["progress"]
        progress.set_supported(True)
        progress.install_cancel(released.set)
        assert released.wait(timeout=2)
        raise InterruptedError("abandoned")

    with patch("agent.context_compressor.call_llm", side_effect=stalled), \
         patch("agent.context_compressor.resolve_task_route", return_value=("primary", "primary-model")):
        result = compressor.compress(messages, fallback_routes=[], stall_timeout=0.01)

    assert result is messages
    assert compressor.logical_attempt_count == 1
    assert compressor.last_compaction_outcome == "fallback_unavailable"
    assert compressor.transcript_outcome == "preserved"


def test_overflow_recovery_consumes_budget_and_never_falls_back():
    compressor = _compressor()
    messages = _messages()
    calls = 0

    def call(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("maximum context length exceeded")

    with patch("agent.context_compressor.call_llm", side_effect=call), \
         patch("agent.context_compressor.resolve_provider_client", side_effect=AssertionError("no fallback")):
        result = compressor.compress(
            messages,
            fallback_routes=[{"provider": "backup", "model": "fallback-model"}],
        )

    assert result is messages
    assert calls == 2
    assert compressor.logical_attempt_count == 2
    assert compressor.fallback_attempted is False
    assert compressor.last_compaction_outcome == "overflow_recovery_failed"


def test_unsupported_progress_reporting_uses_bounded_call_result():
    compressor = _compressor()
    messages = _messages()

    def call(**kwargs):
        kwargs["progress"].set_supported(False)
        return _response()

    with patch("agent.context_compressor.call_llm", side_effect=call):
        result = compressor.compress(messages, stall_timeout=0.01)

    assert result is not messages
    assert compressor.stall_detection_outcome == "unsupported"
    assert compressor.logical_attempt_count == 1


def test_single_use_route_pin_is_consumed_and_context_local():
    route = {"provider": "backup", "model": "fallback-model"}
    assert take_pinned_summary_route() is None
    with pin_summary_route(route):
        assert take_pinned_summary_route() == route
        assert take_pinned_summary_route() is None
        assert consumed_summary_route() == route
    assert take_pinned_summary_route() is None
    assert consumed_summary_route() is None


def test_progress_stream_ignores_empty_frames_and_preserves_completion_metadata():
    class CountingProgress(AuxiliaryRequestProgress):
        def __init__(self):
            super().__init__()
            self.marks = 0

        def mark_progress(self):
            self.marks += 1
            super().mark_progress()

    class Stream:
        def __init__(self, chunks):
            self.chunks = chunks
            self.closed = False

        def __iter__(self):
            return iter(self.chunks)

        def close(self):
            self.closed = True

    chunks = [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="", reasoning_content=None, tool_calls=None),
            finish_reason=None,
        )]),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="hello", reasoning_content=None, tool_calls=None),
            finish_reason=None,
        )]),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=None),
            finish_reason="stop",
        )]),
    ]
    stream = Stream(chunks)
    progress = CountingProgress()

    response = _call_with_progress_stream(
        lambda **_kwargs: stream,
        {"model": "model", "messages": []},
        None,
        progress,
    )

    assert progress.supported is True
    assert progress.marks == 1
    assert response.choices[0].message.content == "hello"
    assert response.choices[0].finish_reason == "stop"
    assert stream.closed is True


def test_late_primary_cannot_overwrite_fallback_state():
    compressor = _compressor()
    messages = _messages()
    primary_released = threading.Event()
    primary_returned = threading.Event()
    calls = 0

    def call(**kwargs):
        nonlocal calls
        calls += 1
        progress = kwargs["progress"]
        if calls == 1:
            progress.set_supported(True)
            progress.install_cancel(primary_released.set)
            assert primary_released.wait(timeout=2)
            primary_returned.set()
            return _response("obsolete")
        progress.set_supported(False)
        return _response("winner")

    with patch("agent.context_compressor.call_llm", side_effect=call), \
         patch("agent.context_compressor.resolve_task_route", return_value=("primary", "primary-model")), \
         patch("agent.context_compressor.resolve_provider_client", side_effect=_available_route):
        result = compressor.compress(
            messages,
            fallback_routes=[{"provider": "backup", "model": "fallback-model"}],
            stall_timeout=0.01,
        )

    assert primary_returned.wait(timeout=2)
    assert result is not messages
    assert compressor._previous_summary == "winner"
    assert compressor.last_fallback_outcome == "succeeded"
    assert compressor.compression_count == 1


def test_stale_fence_cleanup_cannot_clear_newer_cancellation_owner():
    agent = SimpleNamespace(session_id="session-a")
    control = _CompressionControl(agent)
    cancelled = []
    primary = control.publish(1, lambda: cancelled.append("primary"))
    fallback = control.publish(2, lambda: cancelled.append("fallback"), expected=primary)

    control.clear(primary, 1)
    assert control.valid(fallback, 2)
    control.cancel()

    assert cancelled == ["fallback"]
    assert not control.valid(fallback, 2)


def test_session_change_invalidates_fence_without_callback_replacement():
    agent = SimpleNamespace(session_id="session-a")
    control = _CompressionControl(agent)
    fence = control.publish(1, lambda: None)
    assert control.valid(fence, 1)

    agent.session_id = "session-b"

    assert not control.valid(fence, 1)
