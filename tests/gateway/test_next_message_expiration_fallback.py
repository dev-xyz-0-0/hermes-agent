"""Tests for GatewayRunner._finalize_expired_session_before_rotation().

get_or_create_session() silently rotates an expired session to a new
session_id, after which the old session_id/transcript are unreachable
through the session key. This fallback must finalize the OLD cached agent
with the OLD transcript strictly BEFORE that rotation happens — and must
race safely against the background expiry watcher doing the same thing.
"""

import threading
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


def _make_source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        chat_type="dm",
    )


@pytest.mark.asyncio
async def test_expired_session_finalized_before_rotation():
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)

    old_entry = MagicMock()
    old_entry.session_id = "sess-old"

    session_store = MagicMock()
    session_store._entries = {session_key: old_entry}
    session_store._is_session_expired.return_value = True
    session_store._lock = threading.Lock()
    session_store._generate_session_key.return_value = session_key
    session_store.load_transcript.return_value = [
        {"role": "user", "content": "old message"},
    ]
    runner.session_store = session_store

    old_agent = MagicMock()

    # Regression guard: provider code must never run while _agent_cache_lock
    # is held, or this acquire deadlocks/fails.
    def _assert_lock_not_held(*_a, **_kw):
        acquired = runner._agent_cache_lock.acquire(blocking=False)
        assert acquired, "shutdown_memory_provider() ran while _agent_cache_lock was held"
        runner._agent_cache_lock.release()

    old_agent.shutdown_memory_provider.side_effect = _assert_lock_not_held
    with runner._agent_cache_lock:
        runner._agent_cache[session_key] = (old_agent, "sig")

    await runner._finalize_expired_session_before_rotation(source)

    # Finalization must use the OLD transcript, loaded for the OLD
    # session_id, before any rotation call is made.
    session_store.load_transcript.assert_called_once_with("sess-old")
    old_agent.shutdown_memory_provider.assert_called_once_with(
        [{"role": "user", "content": "old message"}]
    )
    old_agent.release_memory_provider.assert_not_called()
    with runner._agent_cache_lock:
        assert session_key not in runner._agent_cache


@pytest.mark.asyncio
async def test_non_expired_session_is_left_untouched():
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)

    entry = MagicMock()
    entry.session_id = "sess-current"

    session_store = MagicMock()
    session_store._entries = {session_key: entry}
    session_store._is_session_expired.return_value = False
    session_store._lock = threading.Lock()
    session_store._generate_session_key.return_value = session_key
    runner.session_store = session_store

    agent = MagicMock()
    with runner._agent_cache_lock:
        runner._agent_cache[session_key] = (agent, "sig")

    await runner._finalize_expired_session_before_rotation(source)

    agent.shutdown_memory_provider.assert_not_called()
    agent.release_memory_provider.assert_not_called()
    with runner._agent_cache_lock:
        assert session_key in runner._agent_cache


@pytest.mark.asyncio
async def test_no_existing_entry_is_a_no_op():
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)

    session_store = MagicMock()
    session_store._entries = {}
    session_store._lock = threading.Lock()
    session_store._generate_session_key.return_value = session_key
    runner.session_store = session_store

    # Should not raise even though there's nothing to finalize.
    await runner._finalize_expired_session_before_rotation(source)

    session_store._is_session_expired.assert_not_called()


@pytest.mark.asyncio
async def test_expiration_race_watcher_vs_fallback_finalizes_exactly_once():
    """The watcher and the next-message fallback both racing to finalize
    the same expired session's cached agent must result in exactly one
    on_session_end-equivalent call, thanks to the agent-level at-most-once
    guarantee."""
    from agent.memory_manager import MemoryManager
    from run_agent import AIAgent

    manager = MagicMock()
    agent = object.__new__(AIAgent)
    agent._memory_manager = manager
    agent._memory_provider_finalized = False
    agent._memory_provider_finalization_lock = threading.Lock()

    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)

    old_entry = MagicMock()
    old_entry.session_id = "sess-old"

    session_store = MagicMock()
    session_store._entries = {session_key: old_entry}
    session_store._is_session_expired.return_value = True
    session_store._lock = threading.Lock()
    session_store._generate_session_key.return_value = session_key
    session_store.load_transcript.return_value = [{"role": "user", "content": "x"}]
    runner.session_store = session_store

    with runner._agent_cache_lock:
        runner._agent_cache[session_key] = (agent, "sig")

    import asyncio

    # Simulate the watcher and the next-message fallback both discovering
    # the expiry concurrently and racing to finalize the same agent.
    await asyncio.gather(
        runner._finalize_expired_session_before_rotation(source),
        runner._finalize_and_evict_cached_agent(session_key, [{"role": "user", "content": "x"}]),
    )

    manager.on_session_end.assert_called_once()
    manager.shutdown_all.assert_called_once()
