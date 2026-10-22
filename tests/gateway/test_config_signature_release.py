"""Test: replacing a cached agent due to a config-signature change must
release the old agent's memory provider (Class C) without ever calling
on_session_end()/shutdown_memory_provider() — the durable conversation is
not ending, only the runtime object is being swapped.

Exercises the real gateway code path (_run_agent's agent-cache block), not
a re-implementation, by swapping in a fake AIAgent via sys.modules, the same
harness pattern used in tests/gateway/test_chat_identity_propagation.py.
"""

import importlib
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


class _FakeAgent:
    """Stand-in for run_agent.AIAgent — records construction, exposes mocked
    release_memory_provider()/shutdown_memory_provider() so tests can assert
    exactly one of them is or isn't called."""

    def __init__(self, **kwargs):
        self.model = kwargs.get("model")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []
        self.release_memory_provider = MagicMock()
        self.shutdown_memory_provider = MagicMock()

    def run_conversation(self, message, conversation_history=None, task_id=None, persist_user_message=None):
        return {"final_response": "ok", "messages": [], "api_calls": 1}


def _make_runner():
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    return runner


@pytest.mark.asyncio
async def test_config_signature_mismatch_releases_old_agent_without_finalizing(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner()
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    session_key = "agent:main:telegram:dm:chat-1"

    # Seed the cache with an old agent under a signature guaranteed to
    # mismatch whatever _run_agent computes for this turn.
    old_agent = _FakeAgent(model="old-model")
    with runner._agent_cache_lock:
        runner._agent_cache[session_key] = (old_agent, "deliberately-stale-signature")

    # Regression guard: release_memory_provider() must never run while
    # _agent_cache_lock is held, or this acquire deadlocks/fails.
    def _assert_lock_not_held():
        acquired = runner._agent_cache_lock.acquire(blocking=False)
        assert acquired, "release_memory_provider() ran while _agent_cache_lock was held"
        runner._agent_cache_lock.release()

    old_agent.release_memory_provider.side_effect = _assert_lock_not_held

    await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    old_agent.release_memory_provider.assert_called_once()
    old_agent.shutdown_memory_provider.assert_not_called()

    # The new agent must now occupy the cache slot (not the old one).
    with runner._agent_cache_lock:
        cached = runner._agent_cache.get(session_key)
    assert cached is not None
    assert cached[0] is not old_agent


@pytest.mark.asyncio
async def test_config_signature_cache_hit_does_not_release(monkeypatch, tmp_path):
    """A cache HIT (identical turn config) must reuse the same agent
    instance across two turns and never call release_memory_provider —
    nothing is being replaced."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner()
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        chat_type="dm",
    )
    session_key = "agent:main:telegram:dm:chat-1"

    # First turn — cache miss, constructs and caches a fresh agent.
    await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )
    with runner._agent_cache_lock:
        first_agent = runner._agent_cache[session_key][0]
    first_agent.release_memory_provider.assert_not_called()

    # Second turn — identical config, must be a cache HIT reusing the same
    # instance, never releasing it.
    await runner._run_agent(
        message="hello again",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    first_agent.release_memory_provider.assert_not_called()
    first_agent.shutdown_memory_provider.assert_not_called()
    with runner._agent_cache_lock:
        assert runner._agent_cache[session_key][0] is first_agent
