"""Regression tests: gateway chat/thread identity must reach AIAgent.

Bug: multi-tenant memory providers (e.g. Holographic) fail closed with
"no chat identity could be resolved" because the gateway had
source.chat_id/source.thread_id available but never passed them into
AIAgent(...). AIAgent then never forwarded them to MemoryManager, so
providers saw platform="telegram" with no chat_id and blocked all
fact_store operations (add, search, list alike).

These tests exercise the real gateway code paths (not a re-implementation
of the fix) by swapping in a FakeAgent that records its constructor kwargs.
"""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session import SessionSource


class RecordingAgent:
    """Stand-in for run_agent.AIAgent that records init kwargs instead of
    doing any real model/tool setup."""

    last_kwargs = None

    def __init__(self, **kwargs):
        RecordingAgent.last_kwargs = kwargs
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

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
    return runner


@pytest.mark.asyncio
async def test_main_turn_passes_chat_id_and_thread_id_to_agent(monkeypatch, tmp_path):
    """The primary per-message AIAgent construction (gateway/run.py's
    _run_agent) must forward source.chat_id/source.thread_id."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    RecordingAgent.last_kwargs = None
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RecordingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner()
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-9",
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-1",
        session_key="agent:main:telegram:group:-1001:17585",
    )

    assert result["final_response"] == "ok"
    assert RecordingAgent.last_kwargs is not None
    assert RecordingAgent.last_kwargs.get("chat_id") == "-1001"
    assert RecordingAgent.last_kwargs.get("thread_id") == "17585"
    assert RecordingAgent.last_kwargs.get("user_id") == "user-9"


@pytest.mark.asyncio
async def test_main_turn_dm_without_thread_id_omits_it(monkeypatch, tmp_path):
    """A DM session has no thread_id — it must come through as None/absent,
    never a stand-in value, so namespace resolution stays correct."""
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "off")

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    RecordingAgent.last_kwargs = None
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = RecordingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    runner = _make_runner()
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-9",
        chat_id="dm-123",
    )

    await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-2",
        session_key="agent:main:telegram:dm-123",
    )

    assert RecordingAgent.last_kwargs.get("chat_id") == "dm-123"
    assert not RecordingAgent.last_kwargs.get("thread_id")
