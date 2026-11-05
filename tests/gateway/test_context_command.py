import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.usage_anchor import capture_usage_anchor

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source(chat_id: str = "chat-a") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id=chat_id,
        chat_type="dm",
    )


def _event(text: str, chat_id: str = "chat-a") -> MessageEvent:
    return MessageEvent(text=text, source=_source(chat_id), message_id="m1")


def _agent(session_id: str, used: int = 97_000):
    return SimpleNamespace(
        model="provider/model",
        session_id=session_id,
        context_compressor=SimpleNamespace(
            context_length=200_000,
            threshold_tokens=100_000,
            threshold_percent=0.5,
            last_prompt_tokens=used,
            last_prompt_tokens_source="provider_usage",
            last_prompt_tokens_observed_at=1.0,
            compression_count=0,
        ),
        _cached_system_prompt="You are Hermes.",
        _memory_store=None,
        tools=[],
    )


def _runner(chat_ids=("chat-a",), *, cached=True):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    entries = {}
    transcripts = {}
    cache = {}
    for index, chat_id in enumerate(chat_ids):
        source = _source(chat_id)
        key = build_session_key(source)
        session_id = f"session-{index}"
        entries[key] = SessionEntry(
            session_key=key,
            session_id=session_id,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            platform=Platform.TELEGRAM,
            chat_type="dm",
            last_prompt_tokens=97_000,
        )
        transcripts[session_id] = [{"role": "user", "content": f"hello {chat_id}"}]
        if cached:
            cache[key] = (_agent(session_id, 97_000 - index * 10_000), "sig")
    runner.session_store = SimpleNamespace(
        _entries=entries,
        _lock=threading.RLock(),
        _ensure_loaded=lambda: None,
        load_transcript=lambda session_id: list(transcripts[session_id]),
    )
    runner._agent_cache = cache
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    return runner


@pytest.mark.asyncio
async def test_context_and_ctx_have_identical_gateway_semantics():
    runner = _runner()
    context = await runner._handle_context_command(_event("/context"))
    alias = await runner._handle_context_command(_event("/ctx"))
    assert context == alias
    assert "Used: 97,000 / 200,000" in context
    assert "Compaction pressure: 97.0%" in context


@pytest.mark.asyncio
async def test_context_all_uses_expanded_view():
    runner = _runner()
    result = await runner._handle_context_command(_event("/context all"))
    assert "Expanded estimates" in result


@pytest.mark.asyncio
async def test_commands_discovery_lists_context_and_ctx():
    runner = _runner()
    pages = [
        await runner._handle_commands_command(_event(f"/commands {page}"))
        for page in range(1, 4)
    ]
    result = "\n".join(pages)
    assert "/context" in result
    assert "/ctx" in result


@pytest.mark.asyncio
async def test_manual_compress_rejects_existing_busy_owner():
    runner = _runner()
    source = _source()
    key = build_session_key(source)
    runner.session_store.get_or_create_session = lambda _source: runner.session_store._entries[key]
    runner._running_agents[key] = object()

    result = await runner._handle_compress_command(_event("/compress"))

    assert "already active" in result


@pytest.mark.asyncio
async def test_stop_targets_registered_compression_owner():
    runner = _runner()
    source = _source()
    key = build_session_key(source)
    runner.session_store.get_or_create_session = lambda _source: runner.session_store._entries[key]
    owner = MagicMock()
    runner._running_agents[key] = owner

    result = await runner._handle_stop_command(_event("/stop"))

    owner.interrupt.assert_called_once_with("Stop requested")
    assert "Force-stopped" in result
    assert key not in runner._running_agents


@pytest.mark.asyncio
async def test_gateway_manual_compress_passes_manual_and_releases_owner(monkeypatch):
    runner = _runner()
    source = _source()
    key = build_session_key(source)
    entry = runner.session_store._entries[key]
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
        for index in range(6)
    ]
    rewritten = []
    runner.session_store.get_or_create_session = lambda _source: entry
    runner.session_store.load_transcript = lambda _session_id: list(history)
    runner.session_store.rewrite_transcript = lambda session_id, messages: rewritten.append((session_id, messages))
    runner.session_store.update_session = lambda *_args, **_kwargs: None
    runner.session_store._save = lambda: None

    class FakeAgent:
        instance = None

        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.interrupted = False
            self.manual = None
            FakeAgent.instance = self

        def _compress_context(self, messages, _system, *, approx_tokens, manual=False):
            self.manual = manual
            return messages[:4], "system"

        def is_interrupted(self):
            return self.interrupted

        def interrupt(self, _message=None):
            self.interrupted = True

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {"api_key": "test"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "model")
    class InlineLoop:
        async def run_in_executor(self, _executor, callback):
            return callback()
    monkeypatch.setattr("gateway.run.asyncio.get_event_loop", lambda: InlineLoop())

    result = await runner._handle_compress_command(_event("/compress"))

    assert "Compressed" in result
    assert FakeAgent.instance.manual is True
    assert key not in runner._running_agents
    assert rewritten




@pytest.mark.asyncio
async def test_fresh_chat_does_not_create_a_session():
    runner = _runner(chat_ids=())
    before = dict(runner.session_store._entries)
    result = await runner._handle_context_command(_event("/context"))
    assert "no active session" in result
    assert runner.session_store._entries == before


@pytest.mark.asyncio
async def test_missing_agent_reports_partial_and_rejects_stale_persisted_usage():
    runner = _runner(cached=False)
    result = await runner._handle_context_command(_event("/context"))
    assert "Partial diagnostics" in result
    assert "provenance/freshness cannot be verified" in result
    assert "Source: local_estimate" in result
    assert "97,000" not in result


@pytest.mark.asyncio
async def test_gateway_restart_uses_only_validated_persisted_anchor():
    runner = _runner(cached=False)
    key = build_session_key(_source())
    session_id = runner.session_store._entries[key].session_id
    history = runner.session_store.load_transcript(session_id)
    live = _agent(session_id)
    live.provider = "provider"
    live.api_mode = "chat"
    live.base_url = ""
    anchor = capture_usage_anchor(live, 12_345, 55, history)
    runner._session_db = SimpleNamespace(
        get_session_model_config_value=lambda sid, key, default=None: anchor
    )
    result = await runner._handle_context_command(_event("/context"))
    assert "12,400" in result
    assert "Source: provider_usage" in result

    history[0]["content"] = "rewritten"
    runner.session_store.load_transcript = lambda _session_id: list(history)
    result = await runner._handle_context_command(_event("/context"))
    assert "12,400" not in result


@pytest.mark.asyncio
async def test_multiple_telegram_chats_are_isolated():
    runner = _runner(chat_ids=("chat-a", "chat-b"))
    first = await runner._handle_context_command(_event("/context", "chat-a"))
    second = await runner._handle_context_command(_event("/context", "chat-b"))
    assert "Used: 97,000" in first
    assert "Used: 87,000" in second


@pytest.mark.asyncio
async def test_holographic_attribution_is_session_and_chat_isolated():
    runner = _runner(chat_ids=("chat-a", "chat-b"))
    first_key = build_session_key(_source("chat-a"))
    second_key = build_session_key(_source("chat-b"))
    first_agent = runner._agent_cache[first_key][0]
    second_agent = runner._agent_cache[second_key][0]
    first_agent._holographic_recall_attribution = {
        "state": "available", "session_id": first_agent.session_id,
        "tokens": 321, "provenance": "injected_fenced_block",
    }
    second_agent._holographic_recall_attribution = {
        "state": "available", "session_id": second_agent.session_id,
        "tokens": 0, "provenance": "no_recall",
    }

    first = await runner._handle_context_command(_event("/context", "chat-a"))
    second = await runner._handle_context_command(_event("/context", "chat-b"))

    assert "Dynamic recall: ~321 tokens" in first
    assert "Dynamic recall: ~0 tokens" in second
    assert "~321 tokens" not in second


@pytest.mark.asyncio
async def test_context_diagnose_reports_only_redacted_holographic_metadata():
    runner = _runner()
    key = build_session_key(_source())
    agent = runner._agent_cache[key][0]
    agent._holographic_recall_attribution = {
        "state": "available", "session_id": agent.session_id,
        "tokens": 44, "provenance": "injected_fenced_block",
        "secret_for_test": "private recalled fact",
    }

    result = await runner._handle_context_command(_event("/context diagnose"))

    assert "Holographic recall: available, ~44 tokens (injected_fenced_block)" in result
    assert "private recalled fact" not in result


@pytest.mark.asyncio
async def test_stale_cached_agent_from_prior_session_is_not_exposed():
    runner = _runner()
    key = build_session_key(_source())
    runner._agent_cache[key] = (_agent("old-session", 88_888), "sig")
    result = await runner._handle_context_command(_event("/context"))
    assert "Partial diagnostics" in result
    assert "88,888" not in result


@pytest.mark.asyncio
async def test_context_is_bounded_and_read_only():
    runner = _runner()
    key = build_session_key(_source())
    agent = runner._agent_cache[key][0]
    agent.tools = [
        {"type": "function", "function": {"name": f"tool_{i}", "description": "x" * 500}}
        for i in range(100)
    ]
    before = dict(vars(agent.context_compressor))
    result = await runner._handle_context_command(_event("/context all"))
    assert len(result) <= 3_500
    assert vars(agent.context_compressor) == before


@pytest.mark.asyncio
async def test_context_rejects_malformed_arguments():
    runner = _runner()
    result = await runner._handle_context_command(_event("/context diagnose extra"))
    assert result.startswith("Usage:")


@pytest.mark.asyncio
async def test_context_diagnose_is_read_only_and_reports_session_evidence():
    runner = _runner()
    key = build_session_key(_source())
    agent = runner._agent_cache[key][0]
    agent._build_system_prompt = MagicMock(side_effect=AssertionError("must not rebuild"))
    before = dict(vars(agent.context_compressor))
    result = await runner._handle_context_command(_event("/context diagnose"))
    assert "Context diagnosis" in result
    assert "Measurement: provider_usage" in result
    assert "Anchor check:" in result
    assert "Warning check:" in result
    assert vars(agent.context_compressor) == before
    agent._build_system_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_context_diagnose_validates_anchor_against_provider_visible_history():
    """session_meta is persisted but never sent to the provider."""
    runner = _runner()
    key = build_session_key(_source())
    agent = runner._agent_cache[key][0]
    history = [
        {"role": "session_meta", "tools": [{"type": "function"}]},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "next"},
    ]
    provider_history = runner._provider_visible_history(history)
    agent._usage_anchor = capture_usage_anchor(agent, 123, 7, provider_history)
    runner.session_store.load_transcript = lambda _session_id: list(history)

    result = await runner._handle_context_command(_event("/context diagnose"))

    assert "Anchor check: PASS" in result

    history[1]["content"] = "rewritten"
    result = await runner._handle_context_command(_event("/context diagnose"))
    assert "Anchor check: FAIL" in result


@pytest.mark.asyncio
async def test_context_diagnose_reloads_persisted_anchor_at_same_boundary():
    runner = _runner(cached=False)
    key = build_session_key(_source())
    session_id = runner.session_store._entries[key].session_id
    history = [
        {"role": "session_meta", "tools": [{"type": "function"}]},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    live = _agent(session_id)
    anchor = capture_usage_anchor(
        live, 123, 7, runner._provider_visible_history(history)
    )
    runner.session_store.load_transcript = lambda _session_id: list(history)
    runner._session_db = SimpleNamespace(
        get_session_model_config_value=lambda _sid, _key, _default=None: anchor
    )

    result = await runner._handle_context_command(_event("/context diagnose"))

    assert "Anchor check: PASS" in result


@pytest.mark.asyncio
async def test_delayed_warning_from_reset_session_is_dropped_deterministically():
    runner = _runner()
    key = build_session_key(_source())
    old_id = runner.session_store._entries[key].session_id

    class Adapter:
        def __init__(self):
            self.sent = []
        async def send(self, chat_id, message, metadata=None):
            self.sent.append((chat_id, message, metadata))

    adapter = Adapter()
    runner.session_store._entries[key].session_id = "new-session"
    delivered = await runner._deliver_session_owned_status(
        session_key=key, session_id=old_id, event_type="context_pressure",
        adapter=adapter, chat_id="chat-a", message="97%", metadata=None,
    )
    assert delivered is False
    assert adapter.sent == []
    assert runner._last_context_warnings[key]["state"] == "dropped_stale"


@pytest.mark.asyncio
async def test_current_session_high_pressure_warning_is_delivered():
    runner = _runner()
    key = build_session_key(_source())
    session_id = runner.session_store._entries[key].session_id

    class Adapter:
        def __init__(self):
            self.sent = []
        async def send(self, chat_id, message, metadata=None):
            self.sent.append((chat_id, message, metadata))

    adapter = Adapter()
    delivered = await runner._deliver_session_owned_status(
        session_key=key, session_id=session_id, event_type="context_pressure",
        adapter=adapter, chat_id="chat-a", message="97%", metadata=None,
    )
    assert delivered is True
    assert adapter.sent == [("chat-a", "97%", None)]
    assert runner._last_context_warnings[key]["state"] == "delivered"


@pytest.mark.asyncio
async def test_active_turn_is_labeled():
    runner = _runner()
    result = await runner._handle_context_command(_event("/context"), turn_active=True)
    assert "active turn may be changing" in result


@pytest.mark.asyncio
async def test_ctx_dispatches_immediately_during_active_turn_without_llm_call():
    runner = _runner()
    key = build_session_key(_source())
    agent = runner._agent_cache[key][0]
    agent.interrupt = MagicMock()
    agent._build_system_prompt = MagicMock(side_effect=AssertionError("must not rebuild"))
    runner._running_agents = {key: agent}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._is_user_authorized = lambda _source: True
    result = await runner._handle_message(_event("/ctx"))
    assert "Used: 97,000" in result
    assert "active turn may be changing" in result
    agent.interrupt.assert_not_called()
    agent._build_system_prompt.assert_not_called()
