"""Lifecycle tests for idempotent AIAgent memory-provider finalization."""

import sqlite3
import threading
from unittest.mock import MagicMock

from agent.memory_manager import MemoryManager


def _agent_with_manager(manager):
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._memory_manager = manager
    agent._memory_provider_finalized = False
    agent._memory_provider_finalization_lock = threading.Lock()
    return agent


def test_repeated_shutdown_finalizes_and_closes_once():
    manager = MagicMock()
    agent = _agent_with_manager(manager)
    transcript = [{"role": "user", "content": "Remember this."}]

    agent.shutdown_memory_provider(transcript)
    agent.shutdown_memory_provider(transcript)

    manager.on_session_end.assert_called_once_with(transcript)
    manager.shutdown_all.assert_called_once_with()


def test_concurrent_shutdown_finalizes_and_closes_once():
    manager = MagicMock()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    start = threading.Barrier(3)

    def block_first_callback(_messages):
        callback_entered.set()
        assert release_callback.wait(timeout=2)

    manager.on_session_end.side_effect = block_first_callback
    agent = _agent_with_manager(manager)
    transcript = [{"role": "user", "content": "Remember this."}]

    def finalize():
        start.wait(timeout=2)
        agent.shutdown_memory_provider(transcript)

    threads = [threading.Thread(target=finalize) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    assert callback_entered.wait(timeout=2)
    release_callback.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    manager.on_session_end.assert_called_once_with(transcript)
    manager.shutdown_all.assert_called_once_with()


def test_session_end_failure_is_terminal_and_still_closes(caplog):
    manager = MagicMock()
    manager.on_session_end.side_effect = RuntimeError("extract failed")
    agent = _agent_with_manager(manager)

    with caplog.at_level("WARNING", logger="run_agent"):
        agent.shutdown_memory_provider([])
        agent.shutdown_memory_provider([])

    manager.on_session_end.assert_called_once_with([])
    manager.shutdown_all.assert_called_once_with()
    assert "Memory provider session finalization failed" in caplog.text


def test_shutdown_failure_is_terminal_and_logged(caplog):
    manager = MagicMock()
    manager.shutdown_all.side_effect = RuntimeError("close failed")
    agent = _agent_with_manager(manager)

    with caplog.at_level("WARNING", logger="run_agent"):
        agent.shutdown_memory_provider([])
        agent.shutdown_memory_provider([])

    manager.on_session_end.assert_called_once_with([])
    manager.shutdown_all.assert_called_once_with()
    assert "Memory provider shutdown failed" in caplog.text


def test_repeated_holographic_finalization_extracts_once(tmp_path, monkeypatch):
    from plugins.memory.holographic import HolographicMemoryProvider

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    db_path = tmp_path / "holographic.db"
    transcript = [
        {"role": "user", "content": "I prefer PostgreSQL for analytics workloads."},
        {"role": "assistant", "content": "Understood."},
    ]

    provider = HolographicMemoryProvider({
        "auto_extract": True,
        "db_path": str(db_path),
        "profile_isolation": True,
    })
    provider.initialize(
        "session-1",
        hermes_home=str(hermes_home),
        platform="telegram",
        chat_id="c1",
        user_id="u1",
        agent_context="primary",
    )
    original_on_session_end = provider.on_session_end
    provider.on_session_end = MagicMock(wraps=original_on_session_end)
    manager = MemoryManager()
    manager.add_provider(provider)
    agent = _agent_with_manager(manager)

    agent.shutdown_memory_provider(transcript)
    agent.shutdown_memory_provider(transcript)

    provider.on_session_end.assert_called_once_with(transcript)
    with sqlite3.connect(db_path) as conn:
        facts = conn.execute(
            "SELECT category, content, namespace FROM facts"
        ).fetchall()
    assert facts == [(
        "user_pref",
        "Prefers PostgreSQL for analytics workloads.",
        "telegram:c1",
    )]
