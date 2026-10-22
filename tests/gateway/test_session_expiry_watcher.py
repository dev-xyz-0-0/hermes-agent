"""Tests for the gateway background session-expiry watcher.

True session expiration is a real boundary (Class A): the watcher must
finalize the cached agent with the real persisted transcript and evict it.
The cached runtime agent lives in _agent_cache (NOT _running_agents, which
is only populated mid-turn and is normally empty by the time the watcher
runs) — this is the bug the watcher fix addresses.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    runner._async_flush_memories = AsyncMock()
    return runner


async def _run_watcher_once(runner, monkeypatch):
    """Run exactly one pass of _session_expiry_watcher's loop body by
    stubbing asyncio.sleep so the initial delay is skipped and the loop
    exits after processing expired entries once."""
    import gateway.run as gateway_run_module

    runner._running = True
    call_count = {"n": 0}

    async def fake_sleep(_seconds):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            runner._running = False

    monkeypatch.setattr(gateway_run_module.asyncio, "sleep", fake_sleep)
    await runner._session_expiry_watcher(interval=5)


@pytest.mark.asyncio
async def test_watcher_finalizes_expired_session_via_agent_cache(monkeypatch):
    runner = _make_runner()

    entry = MagicMock()
    entry.memory_flushed = False
    entry.session_id = "sess-expired"

    session_store = MagicMock()
    session_store._entries = {"key-1": entry}
    session_store._is_session_expired.return_value = True
    session_store._lock = threading.Lock()
    session_store.load_transcript.return_value = [
        {"role": "user", "content": "hello"},
    ]
    runner.session_store = session_store

    agent = MagicMock()

    # Regression guard: provider code must never run while _agent_cache_lock
    # is held, or this acquire deadlocks/fails.
    def _assert_lock_not_held(*_a, **_kw):
        acquired = runner._agent_cache_lock.acquire(blocking=False)
        assert acquired, "shutdown_memory_provider() ran while _agent_cache_lock was held"
        runner._agent_cache_lock.release()

    agent.shutdown_memory_provider.side_effect = _assert_lock_not_held
    with runner._agent_cache_lock:
        runner._agent_cache["key-1"] = (agent, "sig")

    await _run_watcher_once(runner, monkeypatch)

    session_store.load_transcript.assert_called_with("sess-expired")
    agent.shutdown_memory_provider.assert_called_once_with(
        [{"role": "user", "content": "hello"}]
    )
    agent.release_memory_provider.assert_not_called()
    with runner._agent_cache_lock:
        assert "key-1" not in runner._agent_cache
    assert entry.memory_flushed is True


@pytest.mark.asyncio
async def test_watcher_does_not_finalize_agent_only_in_running_agents(monkeypatch):
    """Regression guard: an agent present only in _running_agents (the
    old, almost-always-empty dict) must NOT be finalized by the watcher —
    only _agent_cache (where agents actually live between turns) counts."""
    runner = _make_runner()

    entry = MagicMock()
    entry.memory_flushed = False
    entry.session_id = "sess-expired"

    session_store = MagicMock()
    session_store._entries = {"key-1": entry}
    session_store._is_session_expired.return_value = True
    session_store._lock = threading.Lock()
    session_store.load_transcript.return_value = []
    runner.session_store = session_store

    stray_agent = MagicMock()
    runner._running_agents["key-1"] = stray_agent
    # _agent_cache deliberately left empty.

    await _run_watcher_once(runner, monkeypatch)

    stray_agent.shutdown_memory_provider.assert_not_called()
    assert entry.memory_flushed is True
