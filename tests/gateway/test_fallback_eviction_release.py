"""Test: fallback-model activation evicts the cached agent for the session
and must release its memory provider (Class C) — the durable conversation
continues (the next message retries the primary model), so on_session_end()/
shutdown_memory_provider() must never be called.

Exercises the real gateway code path (_run_agent's fallback-tracking block)
via the same harness pattern as tests/gateway/test_chat_identity_propagation.py.
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


class _FallbackAgent:
    """Stand-in for run_agent.AIAgent that reports having silently switched
    to a fallback model during the turn — the real AIAgent does this when
    the primary provider fails mid-run."""

    last_instance = None
    lock_to_check = None

    def __init__(self, **kwargs):
        # Ignore the requested model — simulate the agent falling back to a
        # different one internally during the turn, like the real AIAgent.
        self.model = "fallback-model-xyz"
        self.provider = "fallback-provider"
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []
        self.shutdown_memory_provider = MagicMock()

        lock = _FallbackAgent.lock_to_check

        def _release(*_a, **_kw):
            if lock is not None:
                acquired = lock.acquire(blocking=False)
                assert acquired, "release_memory_provider() ran while _agent_cache_lock was held"
                lock.release()

        self.release_memory_provider = MagicMock(side_effect=_release)
        _FallbackAgent.last_instance = self

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
async def test_fallback_activation_evicts_and_releases_cached_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FallbackAgent
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

    _FallbackAgent.lock_to_check = runner._agent_cache_lock

    await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key=session_key,
    )

    with runner._agent_cache_lock:
        cached = runner._agent_cache.get(session_key)

    # Fallback activation must evict the cache entry entirely — the next
    # message should retry the primary model from a fresh agent.
    assert cached is None
    assert runner._effective_model == "fallback-model-xyz"

    agent = _FallbackAgent.last_instance
    assert agent is not None
    agent.release_memory_provider.assert_called_once()
    agent.shutdown_memory_provider.assert_not_called()
