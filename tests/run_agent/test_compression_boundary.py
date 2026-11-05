"""Tests for context compression boundary alignment.

Verifies that _align_boundary_backward correctly handles tool result groups
so that parallel tool calls are never split during compression.
"""

import threading
from unittest.mock import patch
from types import SimpleNamespace

from agent.context_compressor import ContextCompressor
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tc(call_id: str) -> dict:
    """Create a minimal tool_call dict."""
    return {"id": call_id, "type": "function", "function": {"name": "test", "arguments": "{}"}}


def _tool_result(call_id: str, content: str = "result") -> dict:
    """Create a tool result message."""
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _assistant_with_tools(*call_ids: str) -> dict:
    """Create an assistant message with tool_calls."""
    return {"role": "assistant", "tool_calls": [_tc(cid) for cid in call_ids], "content": None}


def _make_compressor(**kwargs) -> ContextCompressor:
    defaults = dict(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=3,
        protect_last_n=4,
        quiet_mode=True,
    )
    defaults.update(kwargs)
    with patch("agent.context_compressor.get_model_context_length", return_value=8000):
        return ContextCompressor(**defaults)


def _minimal_agent_for_compression(compressor):
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "session-a"
    agent.model = "test-model"
    agent.platform = "cli"
    agent.context_compressor = compressor
    agent._usage_anchor = {"session_id": "session-a"}
    agent._turn_base_usage_anchor = agent._usage_anchor
    agent._session_db = None
    agent._memory_manager = None
    agent._cached_system_prompt = "system"
    agent._context_pressure_warned = True
    agent._todo_store = SimpleNamespace(format_for_injection=lambda: "")
    agent.flush_memories = lambda *_args, **_kwargs: None
    agent._invalidate_system_prompt = lambda: setattr(agent, "_cached_system_prompt", None)
    agent._build_system_prompt = lambda _system: "rebuilt system"
    return agent


class TestTransactionalCompressionBoundary:
    def test_non_mutating_failure_preserves_usage_anchor_and_session(self):
        compressor = _make_compressor(protect_first_n=2, protect_last_n=2)
        agent = _minimal_agent_for_compression(compressor)
        messages = [{"role": "user", "content": str(i)} for i in range(12)]
        anchor = agent._usage_anchor
        with patch("agent.context_compressor.call_llm", side_effect=Exception("timeout")):
            result, system = agent._compress_context(messages, "system", approx_tokens=7000)
        assert result is messages
        assert system == "system"
        assert agent.session_id == "session-a"
        assert agent._usage_anchor is anchor

    def test_successful_rewrite_invalidates_usage_anchor(self):
        compressor = _make_compressor(protect_first_n=2, protect_last_n=2)
        agent = _minimal_agent_for_compression(compressor)
        messages = [{"role": "user", "content": str(i)} for i in range(12)]
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="valid summary"),
        )])
        with patch("agent.context_compressor.call_llm", return_value=response):
            result, _system = agent._compress_context(messages, "system", approx_tokens=7000)
        assert result is not messages
        assert agent._usage_anchor is None
        assert agent._turn_base_usage_anchor is None

    def test_stall_fallback_does_not_repeat_pre_compression_callbacks(self):
        compressor = _make_compressor(
            model="primary-model", provider="primary",
            protect_first_n=2, protect_last_n=2,
        )
        agent = _minimal_agent_for_compression(compressor)
        agent._fallback_chain = [{"provider": "backup", "model": "fallback-model"}]
        counts = {"flush": 0, "provider": 0}
        agent.flush_memories = lambda *_args, **_kwargs: counts.__setitem__("flush", counts["flush"] + 1)
        agent._memory_manager = SimpleNamespace(
            on_pre_compress=lambda _messages: counts.__setitem__("provider", counts["provider"] + 1)
        )
        messages = [{"role": "user", "content": str(i)} for i in range(12)]
        released = threading.Event()
        calls = 0

        def call(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["progress"].set_supported(True)
                kwargs["progress"].install_cancel(released.set)
                assert released.wait(timeout=2)
                raise InterruptedError("abandoned")
            kwargs["progress"].set_supported(False)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="fallback"),
            )])

        with patch("agent.context_compressor.call_llm", side_effect=call), \
             patch("agent.context_compressor.resolve_task_route", return_value=("primary", "primary-model")), \
             patch("agent.context_compressor.resolve_provider_client", return_value=(object(), "fallback-model")), \
             patch("agent.auxiliary_client.get_compression_stall_timeout", return_value=0.01):
            result, _system = agent._compress_context(messages, "system", approx_tokens=7000)

        assert result is not messages
        assert counts == {"flush": 1, "provider": 1}


# ---------------------------------------------------------------------------
# _align_boundary_backward tests
# ---------------------------------------------------------------------------

class TestAlignBoundaryBackward:
    """Test that compress-end boundary never splits a tool_call/result group."""

    def test_boundary_at_clean_position(self):
        """Boundary after a user message — no adjustment needed."""
        comp = _make_compressor()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "do something"},
            _assistant_with_tools("tc_1"),
            _tool_result("tc_1", "done"),
            {"role": "user", "content": "thanks"},  # idx=6
            {"role": "assistant", "content": "np"},
        ]
        # Boundary at 7, messages[6] = user — no adjustment
        assert comp._align_boundary_backward(messages, 7) == 7

    def test_boundary_after_assistant_with_tools(self):
        """Original case: boundary right after assistant with tool_calls."""
        comp = _make_compressor()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            _assistant_with_tools("tc_1", "tc_2"),  # idx=3
            _tool_result("tc_1"),                    # idx=4
            _tool_result("tc_2"),                    # idx=5
            {"role": "user", "content": "next"},
        ]
        # Boundary at 4, messages[3] = assistant with tool_calls → pull back to 3
        assert comp._align_boundary_backward(messages, 4) == 3

    def test_boundary_in_middle_of_tool_results(self):
        """THE BUG: boundary falls between tool results of the same group."""
        comp = _make_compressor()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "do 5 things"},
            _assistant_with_tools("tc_A", "tc_B", "tc_C", "tc_D", "tc_E"),  # idx=4
            _tool_result("tc_A", "result A"),    # idx=5
            _tool_result("tc_B", "result B"),    # idx=6
            _tool_result("tc_C", "result C"),    # idx=7
            _tool_result("tc_D", "result D"),    # idx=8
            _tool_result("tc_E", "result E"),    # idx=9
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        # Boundary at 8 — in middle of tool results. messages[7] = tool result.
        # Must walk back to idx=4 (the parent assistant).
        assert comp._align_boundary_backward(messages, 8) == 4

    def test_boundary_at_last_tool_result(self):
        """Boundary right after last tool result — messages[idx-1] is tool."""
        comp = _make_compressor()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            _assistant_with_tools("tc_1", "tc_2", "tc_3"),  # idx=3
            _tool_result("tc_1"),    # idx=4
            _tool_result("tc_2"),    # idx=5
            _tool_result("tc_3"),    # idx=6
            {"role": "user", "content": "next"},
        ]
        # Boundary at 7 — messages[6] is last tool result.
        # Walk back: [6]=tool, [5]=tool, [4]=tool, [3]=assistant with tools → idx=3
        assert comp._align_boundary_backward(messages, 7) == 3

    def test_boundary_with_consecutive_tool_groups(self):
        """Two consecutive tool groups — only walk back to the nearest parent."""
        comp = _make_compressor()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            _assistant_with_tools("tc_1"),     # idx=2
            _tool_result("tc_1"),              # idx=3
            {"role": "user", "content": "more"},
            _assistant_with_tools("tc_2", "tc_3"),  # idx=5
            _tool_result("tc_2"),              # idx=6
            _tool_result("tc_3"),              # idx=7
            {"role": "user", "content": "done"},
        ]
        # Boundary at 7 — messages[6] = tool result for tc_2 group
        # Walk back: [6]=tool, [5]=assistant with tools → idx=5
        assert comp._align_boundary_backward(messages, 7) == 5


# ---------------------------------------------------------------------------
# End-to-end: compression must not lose tool results
# ---------------------------------------------------------------------------

class TestCompressionToolResultPreservation:
    """Verify that compress() never silently drops tool results."""

    def test_parallel_tool_results_not_lost(self):
        """The exact scenario that triggered silent data loss before the fix."""
        comp = _make_compressor(protect_first_n=3, protect_last_n=4)

        messages = [
            {"role": "system", "content": "You are helpful."},            # 0
            {"role": "user", "content": "Hello"},                         # 1
            {"role": "assistant", "content": "Hi there!"},                # 2  (end of head)
            {"role": "user", "content": "Read 7 files for me"},           # 3
            _assistant_with_tools("tc_A", "tc_B", "tc_C", "tc_D", "tc_E", "tc_F", "tc_G"),  # 4
            _tool_result("tc_A", "content of file A"),                    # 5
            _tool_result("tc_B", "content of file B"),                    # 6
            _tool_result("tc_C", "content of file C"),                    # 7
            _tool_result("tc_D", "content of file D"),                    # 8
            _tool_result("tc_E", "content of file E"),                    # 9
            _tool_result("tc_F", "content of file F"),                    # 10
            _tool_result("tc_G", "CRITICAL DATA in file G"),              # 11 ← compress_end=15-4=11
            {"role": "user", "content": "Now summarize them"},            # 12
            {"role": "assistant", "content": "Here is the summary..."},   # 13
            {"role": "user", "content": "Thanks"},                        # 14
        ]
        # 15 messages. compress_end = 15 - 4 = 11 (before fix: splits tool group)

        fake_summary = "[Summary of earlier conversation]"
        with patch.object(comp, "_generate_summary", return_value=fake_summary):
            result = comp.compress(messages, current_tokens=7000)

        # After compression, no tool results should be orphaned/lost.
        # All tool results in the result must have a matching assistant tool_call.
        assistant_call_ids = set()
        for msg in result:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id", "")
                    if cid:
                        assistant_call_ids.add(cid)

        tool_result_ids = set()
        for msg in result:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    tool_result_ids.add(cid)

        # Every tool result must have a parent — no orphans
        orphaned = tool_result_ids - assistant_call_ids
        assert not orphaned, f"Orphaned tool results found (data loss!): {orphaned}"

        # Every assistant tool_call must have a real result (not a stub)
        for msg in result:
            if msg.get("role") == "tool":
                assert msg["content"] != "[Result from earlier conversation — see context summary above]", \
                    f"Stub result found for {msg.get('tool_call_id')} — real result was lost"
