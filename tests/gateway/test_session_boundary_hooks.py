"""Tests that on_session_finalize and on_session_reset plugin hooks fire in the gateway."""
import asyncio
from concurrent.futures import Future
from datetime import datetime
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(platform), message_id="m1")


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


def _make_runner(platform: Platform = Platform.TELEGRAM):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._background_tasks = set()
    runner._agent_cache = {}
    runner._async_flush_memories = AsyncMock()

    session_key = build_session_key(_make_source(platform))
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-old",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
        chat_type="dm",
    )
    new_session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-new",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=platform,
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
async def test_reset_finalizes_holographic_on_non_telegram_platform(
    mock_invoke_hook, tmp_path, monkeypatch
):
    """/new finalization is no longer Telegram-only — it must also run for
    other gateway platforms (e.g. Discord) that share this command handler.
    """
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
        platform="discord",
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

    runner = _make_runner(Platform.DISCORD)
    monkeypatch.setattr("gateway.run.ThreadPoolExecutor", _InlineExecutor)
    runner._agent_cache_lock = threading.Lock()
    session_key = build_session_key(_make_source(Platform.DISCORD))
    runner._agent_cache[session_key] = (agent, "sig")
    runner.session_store.load_transcript.return_value = transcript
    runner._async_flush_memories = AsyncMock()

    result = await runner._handle_reset_command(_make_event("/new", Platform.DISCORD))
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
        "discord:c1",
    )


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_telegram_reset_preserves_holographic_chat_isolation(
    mock_invoke_hook, tmp_path, monkeypatch
):
    """Two Telegram chats sharing one DB only see their own reset extracts."""
    from agent.memory_manager import MemoryManager
    from plugins.memory.holographic import HolographicMemoryProvider
    from run_agent import AIAgent

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    db_path = tmp_path / "shared-holographic.db"
    provider_config = {
        "auto_extract": True,
        "db_path": str(db_path),
        "profile_isolation": True,
    }

    def source(chat_id):
        return SessionSource(
            platform=Platform.TELEGRAM,
            user_id="same-profile-user",
            chat_id=chat_id,
            user_name="tester",
            chat_type="dm",
        )

    sources = {chat_id: source(chat_id) for chat_id in ("chat-a", "chat-b")}
    session_keys = {
        chat_id: build_session_key(chat_source)
        for chat_id, chat_source in sources.items()
    }
    transcripts = {
        "a-old": [
            {"role": "user", "content": "I prefer PostgreSQL for analytics workloads."},
            {"role": "assistant", "content": "Understood."},
        ],
        "b-old": [
            {"role": "user", "content": "I prefer SQLite for local prototypes."},
            {"role": "assistant", "content": "Understood."},
        ],
        "b-postgres": [
            {"role": "user", "content": "I prefer PostgreSQL for analytics workloads."},
            {"role": "assistant", "content": "Understood."},
        ],
    }

    def entry(chat_id, session_id):
        now = datetime.now()
        return SessionEntry(
            session_key=session_keys[chat_id],
            session_id=session_id,
            created_at=now,
            updated_at=now,
            origin=sources[chat_id],
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )

    runner = _make_runner()
    monkeypatch.setattr("gateway.run.ThreadPoolExecutor", _InlineExecutor)
    runner._agent_cache_lock = threading.Lock()
    runner.session_store._generate_session_key.side_effect = build_session_key
    runner.session_store._entries = {
        session_keys["chat-a"]: entry("chat-a", "a-old"),
        session_keys["chat-b"]: entry("chat-b", "b-old"),
    }
    runner.session_store.load_transcript.side_effect = lambda session_id: transcripts[
        session_id
    ]
    reset_count = 0

    def reset_session(session_key):
        nonlocal reset_count
        reset_count += 1
        old_entry = runner.session_store._entries[session_key]
        chat_id = old_entry.origin.chat_id
        new_entry = entry(chat_id, f"{chat_id}-new-{reset_count}")
        runner.session_store._entries[session_key] = new_entry
        return new_entry

    runner.session_store.reset_session.side_effect = reset_session

    def cached_agent(chat_id, session_id):
        provider = HolographicMemoryProvider(provider_config)
        provider.initialize(
            session_id,
            hermes_home=str(hermes_home),
            platform="telegram",
            chat_id=chat_id,
            user_id="same-profile-user",
            agent_identity="default",
            agent_context="primary",
        )
        manager = MemoryManager()
        manager.add_provider(provider)
        agent = object.__new__(AIAgent)
        agent._memory_manager = manager
        agent._memory_provider_finalized = False
        agent._memory_provider_finalization_lock = threading.Lock()
        return agent

    def inspect_chat(chat_id, query=None):
        provider = HolographicMemoryProvider(provider_config)
        provider.initialize(
            f"inspect-{chat_id}",
            hermes_home=str(hermes_home),
            platform="telegram",
            chat_id=chat_id,
            user_id="same-profile-user",
            agent_identity="default",
            agent_context="primary",
        )
        try:
            listed = json.loads(
                provider.handle_tool_call("fact_store", {"action": "list", "limit": 100})
            )
            searched = None
            if query is not None:
                searched = json.loads(provider.handle_tool_call(
                    "fact_store",
                    {"action": "search", "query": query, "limit": 100},
                ))
            return listed, searched
        finally:
            provider.shutdown()

    async def reset_chat(chat_id, session_id):
        session_key = session_keys[chat_id]
        agent = cached_agent(chat_id, session_id)
        runner._agent_cache[session_key] = (agent, f"sig-{session_id}")
        result = await runner._handle_reset_command(
            MessageEvent(text="/new", source=sources[chat_id], message_id=session_id)
        )
        assert "Session reset" in result
        assert session_key not in runner._agent_cache

    postgres = "Prefers PostgreSQL for analytics workloads."
    sqlite = "Prefers SQLite for local prototypes."

    await reset_chat("chat-a", "a-old")
    a_list, a_postgres = inspect_chat("chat-a", "PostgreSQL")
    assert [fact["content"] for fact in a_list["facts"]] == [postgres]
    assert [fact["content"] for fact in a_postgres["results"]] == [postgres]

    b_list, b_postgres = inspect_chat("chat-b", "PostgreSQL")
    assert b_list == {"facts": [], "count": 0}
    assert b_postgres == {"results": [], "count": 0}

    await reset_chat("chat-b", "b-old")
    a_list, a_sqlite = inspect_chat("chat-a", "SQLite")
    b_list, b_sqlite = inspect_chat("chat-b", "SQLite")
    _, a_postgres = inspect_chat("chat-a", "PostgreSQL")
    _, b_postgres = inspect_chat("chat-b", "PostgreSQL")
    assert {fact["content"] for fact in a_list["facts"]} == {postgres}
    assert a_sqlite["count"] == 0
    assert a_postgres["count"] == 1
    assert {fact["content"] for fact in b_list["facts"]} == {sqlite}
    assert b_sqlite["count"] == 1
    assert b_postgres["count"] == 0

    runner.session_store._entries[session_keys["chat-b"]] = entry(
        "chat-b", "b-postgres"
    )
    await reset_chat("chat-b", "b-postgres")
    a_list, a_postgres = inspect_chat("chat-a", "PostgreSQL")
    b_list, b_postgres = inspect_chat("chat-b", "PostgreSQL")
    assert [fact["content"] for fact in a_list["facts"]] == [postgres]
    assert a_postgres["count"] == 1
    assert {fact["content"] for fact in b_list["facts"]} == {sqlite, postgres}
    assert b_postgres["count"] == 1

    assert runner.session_store.reset_session.call_count == 3
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT content, namespace FROM facts WHERE content = ? ORDER BY namespace",
            (postgres,),
        ).fetchall()
    assert rows == [
        (postgres, "telegram:chat-a"),
        (postgres, "telegram:chat-b"),
    ]


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

    # Gateway shutdown is resource release only (Class C) — the durable
    # conversation is not ending, so it must release provider resources
    # via release_memory_provider() and must NEVER call
    # shutdown_memory_provider()/on_session_end() on process termination.
    agent1.release_memory_provider.assert_called_once()
    agent2.release_memory_provider.assert_called_once()
    agent1.shutdown_memory_provider.assert_not_called()
    agent2.shutdown_memory_provider.assert_not_called()


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook")
async def test_shutdown_releases_agents_from_agent_cache(mock_invoke_hook):
    """Gateway stop() must release agents living only in _agent_cache.

    _running_agents is normally empty between turns — the persistent
    cache (_agent_cache) is where agents actually live, and shutdown must
    not abandon them unreleased.
    """
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
    runner._running_agents = {}

    agent = MagicMock()
    agent.session_id = "sess-cached"
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {"key-cached": (agent, "sig-1")}

    with patch("gateway.status.remove_pid_file"), \
         patch("gateway.status.write_runtime_status"):
        await runner.stop()

    agent.release_memory_provider.assert_called_once()
    agent.shutdown_memory_provider.assert_not_called()
    assert runner._agent_cache == {}


@pytest.mark.asyncio
@patch("hermes_cli.plugins.invoke_hook", side_effect=Exception("boom"))
async def test_hook_error_does_not_break_reset(mock_invoke_hook):
    """Plugin hook errors must not prevent /new from completing."""
    runner = _make_runner()

    result = await runner._handle_reset_command(_make_event("/new"))

    # Should still return a success message despite hook errors
    assert "Session reset" in result or "New session" in result
