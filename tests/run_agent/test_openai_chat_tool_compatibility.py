from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason, classify_api_error, extract_status_code
from run_agent import AIAgent


TOOL = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Run a command.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _agent(*, provider="openai-api", model="gpt-5.6-sol", tools=True):
    with (
        patch("run_agent.get_tool_definitions", return_value=[TOOL] if tools else []),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            provider=provider,
            model=model,
            api_mode="chat_completions",
            base_url="https://openai-proxy.example/v1",
            api_key="test-key",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.save_trajectories = False
    return agent


def test_openai_gpt5_chat_tools_force_reasoning_none():
    agent = _agent()
    agent.reasoning_config = {"enabled": True, "effort": "high"}

    kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])

    assert kwargs["tools"] == [TOOL]
    assert kwargs["reasoning_effort"] == "none"


def test_openai_gpt5_chat_without_tools_preserves_reasoning_effort():
    agent = _agent(tools=False)
    agent.reasoning_config = {"enabled": True, "effort": "high"}

    kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])

    assert "tools" not in kwargs
    assert kwargs["reasoning_effort"] == "high"


@pytest.mark.parametrize("provider", ["openrouter", "custom", "nous"])
def test_openai_compatible_providers_are_unchanged(provider):
    agent = _agent(provider=provider)

    kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])

    assert "reasoning_effort" not in kwargs


def test_string_only_http_400_is_non_retryable():
    error = RuntimeError(
        "HTTP 400: Function tools with reasoning_effort are not supported "
        "for gpt-5.6-sol in /v1/chat/completions"
    )

    assert extract_status_code(error) == 400
    classified = classify_api_error(error, provider="openai-api", model="gpt-5.6-sol")
    assert classified.reason is FailoverReason.format_error
    assert classified.retryable is False


def test_compatibility_400_fails_after_one_api_call_even_for_large_context():
    agent = _agent()
    agent.compression_enabled = False
    client = MagicMock()
    error = RuntimeError(
        "HTTP 400: Function tools with reasoning_effort are not supported "
        "for gpt-5.6-sol in /v1/chat/completions"
    )
    client.chat.completions.create.side_effect = error
    agent._create_request_openai_client = MagicMock(return_value=client)
    agent._close_request_openai_client = MagicMock()
    agent._persist_session = MagicMock()
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()

    history = [
        {"role": "user", "content": "x" * 100_000},
        {"role": "assistant", "content": "y" * 100_000},
    ]
    result = agent.run_conversation("hello", conversation_history=history)

    assert result["failed"] is True
    assert client.chat.completions.create.call_count == 1


def test_responses_tool_payload_keeps_reasoning():
    agent = _agent()
    agent.api_mode = "codex_responses"
    agent.reasoning_config = {"enabled": True, "effort": "high"}

    kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])

    assert kwargs["tools"]
    assert kwargs["reasoning"]["effort"] == "high"
