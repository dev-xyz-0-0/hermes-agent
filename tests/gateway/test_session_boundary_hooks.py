"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
import asyncio
from concurrent.futures import Future
from datetime import datetime
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


class _InlineExecutor:
    """Executor test double that completes submitted work synchronously."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def submit(self, func, *args, **kwargs):
        future = Future()
        try:
            future.set_result(func(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner._agent_cache = {}
    runner._async_flush_memories = AsyncMock()

    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = new_session_entry
    runner.session_store.reset_session.return_value = new_session_entry
    runner.session_store._entries = {session_key: session_entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._is_user_authorized = lambda _source: True
    runner._format_session_info = lambda: ""

    return runner


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_telegram_reset_dispatches_transcript_to_holographic_session_end(
    mock_invoke_hook, tmp_path, monkeypatch
):
    """Telegram /new finalizes Holographic memory with the old transcript."""
    from agent.memory_manager import MemoryManager
    from plugins.memory.holographic import HolographicMemoryProvider
    from run_agent import AIAgent

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
        "sess-old",
        hermes_home=str(hermes_home),
        platform="telegram",
        chat_id="c1",
        user_id="u1",
        agent_context="primary",
    )
    manager = MemoryManager()
    manager.add_provider(provider)
    agent = object.__new__(AIAgent)
    agent._memory_manager = manager
    agent._memory_provider_finalized = False
    agent._memory_provider_finalization_lock = threading.Lock()

    runner = _make_runner()
    monkeypatch.setattr("gateway.run.ThreadPoolExecutor", _InlineExecutor)
    runner._agent_cache_lock = threading.Lock()
    session_key = build_session_key(_make_source())
    runner._agent_cache[session_key] = (agent, "sig")
    runner.session_store.load_transcript.return_value = transcript
    runner._async_flush_memories = AsyncMock()

    result = await runner._handle_reset_command(_make_event("/new"))
    await asyncio.sleep(0)

    assert "Session reset" in result
    runner.session_store.load_transcript.assert_called_once_with("sess-old")
    runner.session_store.reset_session.assert_called_once_with(session_key)
    assert session_key not in runner._agent_cache

    with sqlite3.connect(db_path) as conn:
        fact = conn.execute(
            "SELECT category, content, namespace FROM facts"
        ).fetchone()
    assert fact == (
        "user_pref",
        "Prefers PostgreSQL for analytics workloads.",
        "telegram:c1",
    )


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_telegram_reset_continues_when_provider_shutdown_fails(
    mock_invoke_hook, caplog, monkeypatch
):
    """A provider failure is logged without preventing reset or eviction."""
    runner = _make_runner()
    monkeypatch.setattr("gateway.run.ThreadPoolExecutor", _InlineExecutor)
    runner._agent_cache_lock = threading.Lock()
    session_key = build_session_key(_make_source())
    agent = MagicMock()
    agent.shutdown_memory_provider.side_effect = RuntimeError("extract failed")
    runner._agent_cache[session_key] = (agent, "sig")
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "old"}]
    runner._async_flush_memories = AsyncMock()

    with caplog.at_level("WARNING", logger="gateway.run"):
        result = await runner._handle_reset_command(_make_event("/new"))
    await asyncio.sleep(0)

    assert "Session reset" in result
    runner.session_store.reset_session.assert_called_once_with(session_key)
    assert session_key not in runner._agent_cache
    assert "Memory provider finalization failed" in caplog.text


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_telegram_reset_finalizes_before_session_reset(
    mock_invoke_hook, monkeypatch
):
    """Transcript loading and finalization both precede session mutation."""
    runner = _make_runner()
    monkeypatch.setattr("gateway.run.ThreadPoolExecutor", _InlineExecutor)
    runner._agent_cache_lock = threading.Lock()
    session_key = build_session_key(_make_source())
    order = []
    transcript = [{"role": "user", "content": "old"}]
    agent = MagicMock()
    agent.shutdown_memory_provider.side_effect = lambda messages: order.append(
        ("finalize", messages)
    )
    runner._agent_cache[session_key] = (agent, "sig")
    runner.session_store.load_transcript.side_effect = lambda session_id: (
        order.append(("load", session_id)) or transcript
    )
    original_new_entry = runner.session_store.reset_session.return_value
    runner.session_store.reset_session.side_effect = lambda key: (
        order.append(("reset", key)) or original_new_entry
    )
    runner._async_flush_memories = AsyncMock()

    await runner._handle_reset_command(_make_event("/new"))
    await asyncio.sleep(0)

    assert order == [
        ("load", "sess-old"),
        ("finalize", transcript),
        ("reset", session_key),
    ]


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_telegram_reset_does_not_evict_replacement_agent(
    mock_invoke_hook, monkeypatch
):
    """Finalization only evicts the agent instance that was captured."""
    runner = _make_runner()
    monkeypatch.setattr("gateway.run.ThreadPoolExecutor", _InlineExecutor)
    runner._agent_cache_lock = threading.Lock()
    session_key = build_session_key(_make_source())
    old_agent = MagicMock()
    replacement_agent = MagicMock()

    def replace_cached_agent(_messages):
        with runner._agent_cache_lock:
            runner._agent_cache[session_key] = (replacement_agent, "new-sig")

    old_agent.shutdown_memory_provider.side_effect = replace_cached_agent
    runner._agent_cache[session_key] = (old_agent, "old-sig")
    runner.session_store.load_transcript.return_value = []

    await runner._handle_reset_command(_make_event("/new"))

    assert runner._agent_cache[session_key][0] is replacement_agent


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_fires_finalize_hook(mock_invoke_hook):
    """/new must fire on_session_finalize with the OLD session id."""
    runner = _make_runner()

    await runner._handle_reset_command(_make_event("/new"))

    mock_invoke_hook.assert_any_call(
        "on_session_finalize", session_id="sess-old", platform="telegram"
    )


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_reset_fires_reset_hook(mock_invoke_hook):
    """/new must fire on_session_reset with the NEW session id."""
    runner = _make_runner()

    await runner._handle_reset_command(_make_event("/new"))

    mock_invoke_hook.assert_any_call(
        "on_session_reset", session_id="sess-new", platform="telegram"
    )


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_finalize_before_reset(mock_invoke_hook):
    """on_session_finalize must fire before on_session_reset."""
    runner = _make_runner()

    await runner._handle_reset_command(_make_event("/new"))

    calls = [c for c in mock_invoke_hook.call_args_list
             if c[0][0] in ("on_session_finalize", "on_session_reset")]
    hook_names = [c[0][0] for c in calls]
    assert hook_names == ["on_session_finalize", "on_session_reset"]


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_shutdown_fires_finalize_for_active_agents(mock_invoke_hook):
    """Gateway stop() must fire on_session_finalize for each active agent."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._background_tasks = set()
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._shutdown_event = MagicMock()
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._exit_reason = "test"

    agent1 = MagicMock()
    agent1.session_id = "sess-a"
    agent2 = MagicMock()
    agent2.session_id = "sess-b"
    runner._running_agents = {"key-a": agent1, "key-b": agent2}

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        await runner.stop()

    finalize_calls = [
        c for c in mock_invoke_hook.call_args_list
        if c[0][0] == "on_session_finalize"
    ]
    session_ids = {c[1]["session_id"] for c in finalize_calls}
    assert session_ids == {"sess-a", "sess-b"}


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook", side_effect=Exception("boom"))
async def test_hook_error_does_not_break_reset(mock_invoke_hook):
    """Plugin hook errors must not prevent /new from completing."""
    runner = _make_runner()

    result = await runner._handle_reset_command(_make_event("/new"))

    # Should still return a success message despite hook errors
    assert "Session reset" in result or "New session" in result
