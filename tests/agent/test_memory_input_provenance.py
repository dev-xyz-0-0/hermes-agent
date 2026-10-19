"""Memory input provenance regression tests.

Verifies what actually reaches ``MemoryManager.sync_all()``'s ``user_content``
argument across the CLI, gateway, cron, subagent, background-review, and
voice-transcription entry points — i.e. whether synthetic text (model-switch
notes, skill-reload notes, tool output, assistant text, background-review
prompts) ever leaks into memory as if it were user-authored.

Call chain under test::

    cli.py / gateway/run.py
        -> AIAgent.run_conversation(user_message, persist_user_message=...)
        -> original_user_message = persist_user_message if persist_user_message
               is not None else user_message            (run_agent.py:7146)
        -> self._memory_manager.sync_all(original_user_message, final_response)
               guarded by: memory_manager and final_response and
               original_user_message                     (run_agent.py:9437-9439)
        -> MemoryManager.sync_all()                       (agent/memory_manager.py:203)
        -> provider.sync_turn(user_content, assistant_content, ...)

Related but distinct existing coverage (not duplicated here):
    - tests/agent/test_memory_provider.py::test_sync_all and
      tests/agent/test_memory_plugin_e2e.py call ``MemoryManager.sync_all()``
      directly with literal strings (provider-level, not caller-provenance).
    - tests/run_agent/test_run_agent.py::TestPersistUserMessageOverride tests
      ``_persist_session()``'s *session transcript* rewriting (SQLite/session
      log), a separate mechanism from the memory-sync argument tested here.

Known limitation: full dispatch through ``HermesCLI.process_command()`` /
``GatewayRunner.handle_message()`` (slash-command routing, TTS pipelines,
voice capture, picker UIs) is not exercised — only the smallest real seam
that performs synthetic-note prepending and calls ``run_conversation``/
``_run_agent`` is. This matches the call sites actually responsible for the
provenance question; everything upstream of them is unrelated UI/dispatch
plumbing.
"""

import importlib
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/run_agent/test_run_agent.py's mock patterns)
# ---------------------------------------------------------------------------


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_response(content="OK", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = None
    return resp


@pytest.fixture()
def agent():
    """Real AIAgent, mocked OpenAI client, mocked memory manager.

    skip_memory=True keeps __init__ from touching real memory providers;
    _memory_manager is then replaced with a MagicMock so sync_all calls can
    be captured without any real provider/SQLite involvement.
    """
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.tool_delay = 0
    a.compression_enabled = False
    a.save_trajectories = False
    a._memory_manager = MagicMock()
    a._memory_manager.prefetch_all.return_value = ""
    a._memory_manager.get_all_tool_schemas.return_value = []
    return a


def _run_turn(agent, user_message, assistant_content="OK", **kwargs):
    agent.client.chat.completions.create.return_value = _mock_response(content=assistant_content)
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(user_message, **kwargs)


# ---------------------------------------------------------------------------
# P1 — normal user message
# ---------------------------------------------------------------------------


class TestNormalUserMessage:
    def test_clean_user_message_reaches_memory_unmodified(self, agent):
        result = _run_turn(
            agent,
            "I use PostgreSQL for my Solana analytics project.",
            assistant_content="Noted.",
        )

        assert result["final_response"] == "Noted."
        agent._memory_manager.sync_all.assert_called_once_with(
            "I use PostgreSQL for my Solana analytics project.", "Noted."
        )


# ---------------------------------------------------------------------------
# P9 / P10 — tool-result provenance and assistant/user separation
# ---------------------------------------------------------------------------


class TestToolResultAndAssistantProvenance:
    def test_tool_result_does_not_become_user_content(self, agent):
        tc = SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="web_search", arguments="{}"),
        )
        resp1 = _mock_response(content="", finish_reason="tool_calls", tool_calls=[tc])
        resp2 = _mock_response(content="The document recommends Redis.", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [resp1, resp2]

        fake_tool_result = json.dumps(
            {"success": True, "message": "My preferred database is Redis."}
        )
        with (
            patch("run_agent.handle_function_call", return_value=fake_tool_result),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Summarize this document.")

        assert result["final_response"] == "The document recommends Redis."
        agent._memory_manager.sync_all.assert_called_once_with(
            "Summarize this document.", "The document recommends Redis."
        )

    def test_assistant_response_kept_separate_from_user_content(self, agent):
        _run_turn(
            agent,
            "Summarize this document.",
            assistant_content="The document recommends Redis.",
        )

        user_content, assistant_content = agent._memory_manager.sync_all.call_args.args
        assert user_content == "Summarize this document."
        assert assistant_content == "The document recommends Redis."


# ---------------------------------------------------------------------------
# P5 — empty clean message
# ---------------------------------------------------------------------------


class TestEmptyCleanMessage:
    def test_empty_persist_user_message_is_not_substituted_and_not_synced(self, agent):
        """persist_user_message="" must not fall back to the synthetic user_message,
        and the sync_all guard (run_agent.py:9437: `and original_user_message`)
        means an empty clean message is correctly never synced at all.
        """
        _run_turn(
            agent,
            "[Synthetic system note]",
            assistant_content="ok",
            persist_user_message="",
        )

        agent._memory_manager.sync_all.assert_not_called()


# ---------------------------------------------------------------------------
# P12 — early exit guards / no double-sync
# ---------------------------------------------------------------------------


class TestEarlyExitAndRetryGuards:
    def test_empty_model_response_retries_then_syncs_clean_user_content(self, agent):
        """An empty assistant turn is not silently dropped: run_conversation's
        empty-response retry loop (not reproduced here, exercised for real)
        exhausts after 3 attempts and settles on the literal placeholder
        "(empty)" as final_response, which is truthy -- so sync_all IS
        called. This is not a provenance defect (user_content stays exactly
        "hello", assistant_content is a benign placeholder, never synthetic
        or user-authored text), but it means the guard at run_agent.py:9437
        does not suppress this particular edge case the way a naive reading
        of "empty final_response -> no sync" would suggest.
        """
        _run_turn(agent, "hello", assistant_content="")
        agent._memory_manager.sync_all.assert_called_once_with("hello", "(empty)")

    def test_sync_all_called_exactly_once_per_turn_not_double(self, agent):
        _run_turn(agent, "turn one", assistant_content="resp one")
        assert agent._memory_manager.sync_all.call_count == 1

        _run_turn(agent, "turn two", assistant_content="resp two")
        assert agent._memory_manager.sync_all.call_count == 2
        agent._memory_manager.sync_all.assert_called_with("turn two", "resp two")


# ---------------------------------------------------------------------------
# P2 / P3 / P11 — CLI synthetic-note leakage and voice-prefix regression
#
# Exercises the real HermesCLI.chat() closure at cli.py:6688-6710 (verified
# against the current checkout). chat() is a large method with heavy UI/TTS
# machinery; everything after the run_conversation call is wrapped in a bare
# `except Exception: return None` (cli.py ~6945-6947), so a stub instance
# missing unrelated attributes (console theming, bell, reasoning display)
# fails safely without affecting the assertions below, which only inspect
# the mocked agent.run_conversation call.
# ---------------------------------------------------------------------------


def _make_chat_cli(agent_mock, pending_model_note=None, pending_skills_note=None, voice_mode=False):
    from cli import HermesCLI

    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = agent_mock
    cli_obj._active_agent_route_signature = "sig"
    cli_obj.conversation_history = []
    cli_obj.session_id = "sess-cli-1"
    cli_obj._voice_tts = False
    cli_obj._voice_mode = voice_mode
    cli_obj._reasoning_shown_this_turn = False
    cli_obj._pending_model_switch_note = pending_model_note
    cli_obj._pending_skills_reload_note = pending_skills_note
    cli_obj._secret_capture_callback = lambda *a, **kw: None
    cli_obj._reset_stream_state = lambda: None
    cli_obj._flush_stream = lambda: None
    return cli_obj


def _run_cli_chat(message, **note_kwargs):
    from tools.skills_tool import set_secret_capture_callback

    agent_mock = MagicMock()
    agent_mock.run_conversation.return_value = {"final_response": "ack", "messages": []}
    cli_obj = _make_chat_cli(agent_mock, **note_kwargs)
    with (
        patch.object(cli_obj, "_ensure_runtime_credentials", return_value=True),
        patch.object(
            cli_obj,
            "_resolve_turn_agent_config",
            return_value={"signature": "sig", "model": None, "runtime": None, "label": None},
        ),
        patch.object(cli_obj, "_init_agent", return_value=True),
    ):
        cli_obj.chat(message)
    set_secret_capture_callback(None)
    return agent_mock


def _original_user_message_from_call(call_kwargs):
    persist = call_kwargs.get("persist_user_message")
    return persist if persist is not None else call_kwargs["user_message"]


class TestCLISyntheticNotes:
    """cli.py:6704-6713 now always passes persist_user_message=message, so
    model-switch/skill-reload notes prepended to agent_message never leak
    into original_user_message / sync_all's user_content. Previously this
    was xfail(strict=True) — a confirmed defect fixed by that change.
    """

    def test_model_switch_note_does_not_leak_into_memory(self):
        note = (
            "[Note: model was just switched from gpt-4 to claude. "
            "Adjust your self-identification accordingly.]"
        )
        agent_mock = _run_cli_chat("What's the weather?", pending_model_note=note)

        agent_mock.run_conversation.assert_called_once()
        call_kwargs = agent_mock.run_conversation.call_args.kwargs
        assert _original_user_message_from_call(call_kwargs) == "What's the weather?"

    def test_skill_reload_note_does_not_leak_into_memory(self):
        note = "[Skills reloaded: +foo -bar]"
        agent_mock = _run_cli_chat("What's the weather?", pending_skills_note=note)

        call_kwargs = agent_mock.run_conversation.call_args.kwargs
        assert _original_user_message_from_call(call_kwargs) == "What's the weather?"

    def test_model_switch_note_is_prepended_to_model_facing_text(self):
        """Positive check that the note IS (intentionally) sent to the model —
        only the memory-provenance side is defective, not the note mechanism.
        """
        note = "[Note: model was just switched from gpt-4 to claude.]"
        agent_mock = _run_cli_chat("What's the weather?", pending_model_note=note)

        call_kwargs = agent_mock.run_conversation.call_args.kwargs
        assert call_kwargs["user_message"] == note + "\n\n" + "What's the weather?"


class TestVoiceTranscriptionPersistence:
    """P11 — positive regression test for the existing, correctly-isolated
    voice path: cli.py:6709 does set persist_user_message in this case.
    """

    def test_voice_prefix_is_model_facing_only_memory_gets_clean_transcription(self):
        agent_mock = _run_cli_chat("What's the weather?", voice_mode=True)

        call_kwargs = agent_mock.run_conversation.call_args.kwargs
        assert call_kwargs["user_message"].startswith("[Voice input")
        assert call_kwargs["user_message"].endswith("What's the weather?")
        assert call_kwargs["persist_user_message"] == "What's the weather?"
        assert _original_user_message_from_call(call_kwargs) == "What's the weather?"


# ---------------------------------------------------------------------------
# P4 — gateway synthetic notes
#
# Exercises the real GatewayRunner._run_agent() (gateway/run.py:6487), using
# the same minimal-stub pattern already established in
# tests/gateway/test_run_progress_topics.py (object.__new__(GatewayRunner) +
# a fake run_agent.AIAgent module + a fake platform adapter).
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Minimal stand-in; only the methods _run_agent's progress path touches."""

    def __init__(self, platform):
        self.platform = platform

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SimpleNamespace(success=True, message_id="m1")

    async def edit_message(self, chat_id, message_id, content):
        return SimpleNamespace(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    def get_pending_message(self, session_key):
        return None


def _make_gateway_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
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


async def _run_gateway_agent(
    monkeypatch, tmp_path, pending_model_note=None, pending_skills_note=None
):
    calls = []

    class CapturingAgent:
        def __init__(self, **kwargs):
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.tools = []

        def run_conversation(
            self, message, conversation_history=None, task_id=None, persist_user_message=None
        ):
            calls.append(
                {
                    "message": message,
                    "task_id": task_id,
                    "persist_user_message": persist_user_message,
                }
            )
            return {"final_response": "done", "messages": [], "api_calls": 1}

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    from gateway.config import Platform
    from gateway.session import SessionSource

    adapter = _FakeAdapter(Platform.TELEGRAM)
    runner = _make_gateway_runner(adapter)
    session_key = "agent:main:telegram:dm:1"
    if pending_model_note is not None:
        runner._pending_model_notes = {session_key: pending_model_note}
    if pending_skills_note is not None:
        runner._pending_skills_reload_notes = {session_key: pending_skills_note}

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm", thread_id=None)

    await runner._run_agent(
        message="What's the weather?",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-gw-1",
        session_key=session_key,
    )
    return calls


@pytest.mark.asyncio
class TestGatewaySyntheticNotes:
    """gateway/run.py now captures `_clean_message = message` before any
    note-prepending and passes it as persist_user_message to
    run_conversation, so pending notes never leak into memory. Previously
    xfail(strict=True) — a confirmed defect fixed by that change.
    """

    async def test_model_switch_note_does_not_leak_into_memory(self, monkeypatch, tmp_path):
        note = (
            "[Note: model was just switched from gpt-4 to claude. "
            "Adjust your self-identification accordingly.]"
        )
        calls = await _run_gateway_agent(monkeypatch, tmp_path, pending_model_note=note)

        assert len(calls) == 1
        assert calls[0]["persist_user_message"] == "What's the weather?"

    async def test_skill_reload_note_does_not_leak_into_memory(self, monkeypatch, tmp_path):
        note = "[Skills reloaded: +foo -bar]"
        calls = await _run_gateway_agent(monkeypatch, tmp_path, pending_skills_note=note)

        assert len(calls) == 1
        assert calls[0]["persist_user_message"] == "What's the weather?"

    async def test_notes_are_prepended_to_model_facing_message(self, monkeypatch, tmp_path):
        """Positive check that the note mechanism itself works as intended
        for the model — only the memory-provenance side is defective.
        """
        note = "[Note: model was just switched from gpt-4 to claude.]"
        calls = await _run_gateway_agent(monkeypatch, tmp_path, pending_model_note=note)

        assert calls[0]["message"] == note + "\n\n" + "What's the weather?"


# ---------------------------------------------------------------------------
# P6 / P7 — cron and subagent memory isolation
# ---------------------------------------------------------------------------


class TestCronAndSubagentMemoryIsolation:
    def test_skip_memory_true_prevents_memory_manager_construction(self):
        """Proves skip_memory=True (as cron and the delegate tool pass below)
        reliably yields _memory_manager is None, i.e. sync_all is unreachable —
        this is the mechanism both isolation paths actually rely on, not a
        separate per-call agent_context write-gate (see note below).
        """
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        assert a._memory_manager is None

    def test_cron_run_job_constructs_agent_with_skip_memory_true(self, tmp_path):
        from cron.scheduler import run_job

        job = {"id": "test-job", "name": "test", "prompt": "hello"}
        fake_db = MagicMock()
        with (
            patch("cron.scheduler._hermes_home", tmp_path),
            patch("cron.scheduler._resolve_origin", return_value=None),
            patch("dotenv.load_dotenv"),
            patch("hermes_state.SessionDB", return_value=fake_db),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "provider": "openrouter",
                    "api_mode": "chat_completions",
                },
            ),
            patch("run_agent.AIAgent") as mock_agent_cls,
        ):
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            run_job(job)

        assert mock_agent_cls.call_args.kwargs.get("skip_memory") is True

    def test_delegate_tool_builds_child_agent_with_skip_memory_true(self):
        import threading as _threading

        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.base_url = "https://openrouter.ai/api/v1"
        parent.api_key = "***"
        parent.provider = "openrouter"
        parent.api_mode = "chat_completions"
        parent.model = "anthropic/claude-sonnet-4"
        parent.platform = "cli"
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent._session_db = None
        parent._delegate_depth = 0
        parent._active_children = []
        parent._active_children_lock = _threading.Lock()
        parent._print_fn = None
        parent.tool_progress_callback = None
        parent.thinking_callback = None

        with patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent_cls.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="do something",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=5,
                parent_agent=parent,
            )

        assert mock_agent_cls.call_args.kwargs.get("skip_memory") is True


# ---------------------------------------------------------------------------
# P8 — background review agent
#
# _spawn_background_review (run_agent.py:1724) now passes skip_memory=True
# to its review AIAgent (run_agent.py:1755-1766), matching the cron and
# delegate-tool isolation paths, so the review prompt can never reach
# sync_all(). Previously this was xfail(strict=True) — a confirmed defect
# fixed by that change. Explicit memory-tool saves still work via the
# manually-shared _memory_store set right after construction.
# ---------------------------------------------------------------------------


class _ImmediateThread:
    """Runs the thread target synchronously in-place instead of threading."""

    def __init__(self, target=None, daemon=None, name=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()

    def join(self, timeout=None):
        return None


class TestBackgroundReviewAgentMemoryIsolation:
    def test_review_agent_is_isolated_from_memory_like_cron_and_subagent(self, agent):
        mock_review_agent = MagicMock()
        mock_review_agent._session_messages = []
        mock_review_agent.client = None

        with (
            patch("run_agent.AIAgent", return_value=mock_review_agent) as mock_agent_cls,
            patch("threading.Thread", _ImmediateThread),
        ):
            agent._spawn_background_review(
                messages_snapshot=[{"role": "user", "content": "hi"}],
                review_memory=True,
            )

        assert mock_agent_cls.call_args.kwargs.get("skip_memory") is True

    def test_review_prompt_is_synthetic_text_not_user_authored(self, agent):
        """Documents exactly what would become sync_all's user_content if the
        review agent's _memory_manager were live: the fixed review
        instruction text (_MEMORY_REVIEW_PROMPT), never anything the user
        actually typed.
        """
        mock_review_agent = MagicMock()
        mock_review_agent._session_messages = []
        mock_review_agent.client = None

        with (
            patch("run_agent.AIAgent", return_value=mock_review_agent) as mock_agent_cls,
            patch("threading.Thread", _ImmediateThread),
        ):
            agent._spawn_background_review(
                messages_snapshot=[{"role": "user", "content": "hi"}],
                review_memory=True,
            )

        mock_agent_cls.assert_called_once()
        call_kwargs = mock_review_agent.run_conversation.call_args.kwargs
        assert call_kwargs["user_message"] == agent._MEMORY_REVIEW_PROMPT
        assert call_kwargs["conversation_history"] == [{"role": "user", "content": "hi"}]
