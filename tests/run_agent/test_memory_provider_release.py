"""Lifecycle tests for AIAgent.release_memory_provider() (Class C — resource
release only, never on_session_end) and its mutual exclusivity with
shutdown_memory_provider() (Class A — real session boundary).

Mirrors tests/run_agent/test_memory_provider_shutdown.py.
"""

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


def test_repeated_release_closes_once_and_never_finalizes():
    manager = MagicMock()
    agent = _agent_with_manager(manager)

    agent.release_memory_provider()
    agent.release_memory_provider()

    manager.shutdown_all.assert_called_once_with()
    manager.on_session_end.assert_not_called()


def test_concurrent_release_closes_once():
    manager = MagicMock()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    start = threading.Barrier(3)

    def block_first_callback():
        callback_entered.set()
        assert release_callback.wait(timeout=2)

    manager.shutdown_all.side_effect = block_first_callback
    agent = _agent_with_manager(manager)

    def release():
        start.wait(timeout=2)
        agent.release_memory_provider()

    threads = [threading.Thread(target=release) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    assert callback_entered.wait(timeout=2)
    release_callback.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    manager.shutdown_all.assert_called_once_with()
    manager.on_session_end.assert_not_called()


def test_release_failure_is_terminal_and_logged(caplog):
    manager = MagicMock()
    manager.shutdown_all.side_effect = RuntimeError("close failed")
    agent = _agent_with_manager(manager)

    with caplog.at_level("WARNING", logger="run_agent"):
        agent.release_memory_provider()
        agent.release_memory_provider()

    manager.shutdown_all.assert_called_once_with()
    manager.on_session_end.assert_not_called()
    assert "Memory provider release failed" in caplog.text


def test_repeated_holographic_release_closes_once_without_extracting(tmp_path, monkeypatch):
    from plugins.memory.holographic import HolographicMemoryProvider

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    db_path = tmp_path / "holographic.db"

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

    agent.release_memory_provider()
    agent.release_memory_provider()

    provider.on_session_end.assert_not_called()


def test_shutdown_then_release_only_finalizes_once():
    """Whichever of shutdown_memory_provider()/release_memory_provider() runs
    first wins; the other must be a true no-op touching neither method."""
    manager = MagicMock()
    agent = _agent_with_manager(manager)
    transcript = [{"role": "user", "content": "Remember this."}]

    agent.shutdown_memory_provider(transcript)
    agent.release_memory_provider()

    manager.on_session_end.assert_called_once_with(transcript)
    manager.shutdown_all.assert_called_once_with()


def test_release_then_shutdown_never_finalizes():
    """If release wins the race, a later shutdown call must not retroactively
    call on_session_end — finalization is terminal and at-most-once."""
    manager = MagicMock()
    agent = _agent_with_manager(manager)
    transcript = [{"role": "user", "content": "Remember this."}]

    agent.release_memory_provider()
    agent.shutdown_memory_provider(transcript)

    manager.shutdown_all.assert_called_once_with()
    manager.on_session_end.assert_not_called()


def test_cross_method_race_calls_shutdown_all_exactly_once():
    """Concurrently racing shutdown_memory_provider() against
    release_memory_provider() on the same agent must result in exactly one
    outcome: either {on_session_end + shutdown_all} once each, or
    {shutdown_all} alone once — never both on_session_end and shutdown_all
    called twice, never neither."""
    manager = MagicMock()
    entered = threading.Event()
    release_gate = threading.Event()
    start = threading.Barrier(3)

    def block_shutdown_all():
        entered.set()
        assert release_gate.wait(timeout=2)

    manager.shutdown_all.side_effect = block_shutdown_all
    agent = _agent_with_manager(manager)
    transcript = [{"role": "user", "content": "Remember this."}]

    def do_shutdown():
        start.wait(timeout=2)
        agent.shutdown_memory_provider(transcript)

    def do_release():
        start.wait(timeout=2)
        agent.release_memory_provider()

    t1 = threading.Thread(target=do_shutdown)
    t2 = threading.Thread(target=do_release)
    t1.start()
    t2.start()
    start.wait(timeout=2)
    assert entered.wait(timeout=2)
    release_gate.set()
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert not t1.is_alive()
    assert not t2.is_alive()

    manager.shutdown_all.assert_called_once_with()
    # on_session_end was called either exactly once (shutdown won the race)
    # or never (release won) — never more than once.
    assert manager.on_session_end.call_count in (0, 1)
    if manager.on_session_end.call_count == 1:
        manager.on_session_end.assert_called_once_with(transcript)
