"""Tests for the gateway /branch slash command.

Tests GatewayRunner._handle_branch_command — forking the current session
into a new independent copy while the original remains resumable.
"""

import threading
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, build_session_key


def _make_event(text="/branch", platform=Platform.TELEGRAM,
                 user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner(session_db, current_session_id, event):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._session_db = session_db
    runner.config = {}

    session_key = build_session_key(event.source)
    current_entry = MagicMock()
    current_entry.session_id = current_session_id
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = current_entry
    mock_store.load_transcript.return_value = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    switched_entry = MagicMock()
    switched_entry.session_id = "new-branch-session"
    mock_store.switch_session.return_value = switched_entry
    runner.session_store = mock_store
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()

    return runner, session_key


@pytest.mark.asyncio
async def test_branch_releases_and_evicts_cached_agent_without_finalizing(tmp_path):
    """/branch is a session fork (Class B), not a boundary: the original
    session's cached agent must be evicted and released, but
    on_session_end()/shutdown_memory_provider() must NEVER be called —
    the original remains resumable.
    """
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("current_session_001", "telegram")

    event = _make_event()
    runner, session_key = _make_runner(db, "current_session_001", event)

    cached_agent = MagicMock()
    runner._agent_cache[session_key] = (cached_agent, "sig")

    # Regression guard: release_memory_provider() must never run while
    # _agent_cache_lock is held, or this acquire deadlocks/fails.
    def _assert_lock_not_held():
        acquired = runner._agent_cache_lock.acquire(blocking=False)
        assert acquired, "release_memory_provider() ran while _agent_cache_lock was held"
        runner._agent_cache_lock.release()

    cached_agent.release_memory_provider.side_effect = _assert_lock_not_held

    result = await runner._handle_branch_command(event)

    cached_agent.release_memory_provider.assert_called_once()
    cached_agent.shutdown_memory_provider.assert_not_called()
    assert session_key not in runner._agent_cache
    assert "Branched" in result
    db.close()


@pytest.mark.asyncio
async def test_branch_with_no_cached_agent_still_switches(tmp_path):
    """Branching without a cached agent must not error and still forks."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("current_session_001", "telegram")

    event = _make_event()
    runner, session_key = _make_runner(db, "current_session_001", event)

    result = await runner._handle_branch_command(event)

    assert "Branched" in result
    assert session_key not in runner._agent_cache
    db.close()
