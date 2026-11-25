import asyncio
import inspect
import json
import os
import sys
from types import SimpleNamespace

import pytest

from agent import qoder_adapter
from hermes_cli.model_switch import switch_model
from hermes_cli.runtime_provider import resolve_runtime_provider


class StreamEvent(SimpleNamespace):
    pass


class AssistantMessage(SimpleNamespace):
    pass


class TextBlock(SimpleNamespace):
    pass


class ToolUseBlock(SimpleNamespace):
    pass


class ResultMessage(SimpleNamespace):
    pass


@pytest.fixture
def qoder(monkeypatch):
    clients = []
    output = [
        StreamEvent(event={
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"},
        }),
        AssistantMessage(
            model="qoder-model", usage={"credits": 0.2, "request_id": "abc"},
            content=[TextBlock(text="Hello")],
        ),
        ResultMessage(
            subtype="success", is_error=False, error_code=None, result="Hello",
            usage={"input_tokens": 11, "output_tokens": 2}, total_credits=0.2,
        ),
    ]

    class Client:
        def __init__(self, options):
            self.options = options
            self.prompt = None
            self.interrupted = False
            self.disconnected = False
            clients.append(self)

        async def connect(self, prompt=None):
            self.prompt = prompt

        async def get_available_models(self):
            return [
                {"value": "qoder-model", "isEnabled": True},
                {"value": "disabled", "isEnabled": False},
            ]

        async def receive_response(self):
            for item in output:
                if item is None:
                    await asyncio.sleep(60)
                else:
                    yield item

        async def interrupt(self):
            self.interrupted = True

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setenv("HERMES_QODER_PREVIEW", "1")
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "test-token")
    monkeypatch.setitem(sys.modules, "qoder_agent_sdk", SimpleNamespace(
        QoderSDKClient=Client, QoderAgentOptions=SimpleNamespace, access_token=lambda value: value,
        StreamEvent=StreamEvent, AssistantMessage=AssistantMessage, TextBlock=TextBlock,
        ToolUseBlock=ToolUseBlock, ResultMessage=ResultMessage,
    ))
    return clients, output


def test_qoder_preview_credentials_and_model_selection(qoder, monkeypatch):
    assert resolve_runtime_provider(requested="qoder")["api_mode"] == "qoder_sdk"
    assert resolve_runtime_provider(requested="qoder")["api_key"] != "test-token"
    args = dict(current_provider="openrouter", current_model="other")
    assert switch_model("qoder:qoder-model", **args).success
    assert switch_model("qoder", **args).new_model == qoder_adapter.DEFAULT_MODEL
    assert not switch_model("qoder:disabled", **args).success
    monkeypatch.delenv("QODER_PERSONAL_ACCESS_TOKEN")
    assert "QODER_PERSONAL_ACCESS_TOKEN" in switch_model("qoder", **args).error_message
    monkeypatch.delenv("HERMES_QODER_PREVIEW")
    assert "HERMES_QODER_PREVIEW" in switch_model("qoder", **args).error_message


def test_qoder_request_replays_text_and_streams_once(qoder):
    clients, output = qoder
    output.insert(2, AssistantMessage(model="qoder-model", content=[], usage={"credits": 0.2, "request_id": "abc"}))
    deltas = []
    response = qoder_adapter.request(
        [
            {"role": "system", "content": "System instruction"},
            {"role": "user", "content": [
                {"type": "text", "text": "First"}, {"type": "image_url", "image_url": "omit me"},
            ]},
            {"role": "assistant", "content": "Prior answer"},
            {"role": "user", "content": "Follow-up"},
        ],
        qoder_adapter.DEFAULT_MODEL, "test-token", deltas.append, lambda: False,
    )
    client = clients[0]
    history = json.loads(client.prompt.split("\n", 1)[1])
    assert [turn["content"] for turn in history] == ["First", "Prior answer", "Follow-up"]
    assert client.options.system_prompt == "System instruction"
    assert client.options.model is None
    assert client.options.tools == client.options.skills == client.options.setting_sources == []
    assert client.options.mcp_servers == {}
    assert client.options.proxy == os.environ.get("HTTPS_PROXY")
    assert client.disconnected
    assert deltas == ["Hello"]
    assert response.choices[0].message.content == "Hello"
    assert response.usage.total_tokens == 13
    assert response.qoder_credits == 0.2


def test_qoder_client_uses_lowercase_proxy_fallback(qoder, monkeypatch, tmp_path):
    clients, _ = qoder
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("https_proxy", "http://proxy.example:8080")

    qoder_adapter._client(qoder_adapter._sdk(), "test-token", str(tmp_path))

    assert clients[-1].options.proxy == "http://proxy.example:8080"


def test_qoder_workspace_paths_stay_inside_root(tmp_path):
    inside = qoder_adapter._workspace_args("read_file", {"path": "notes.txt"}, tmp_path)
    assert inside["path"] == str(tmp_path / "notes.txt")
    with pytest.raises(ValueError, match="outside the Qoder workspace"):
        qoder_adapter._workspace_args("read_file", {"path": "../secret.txt"}, tmp_path)


def test_qoder_workspace_reads_dispatch_through_hermes(monkeypatch, tmp_path):
    class FakeSDK:
        @staticmethod
        def tool(name, description, schema):
            return lambda handler: SimpleNamespace(name=name, handler=handler)

        @staticmethod
        def create_sdk_mcp_server(name, tools):
            return SimpleNamespace(name=name, tools=tools)

    dispatched = []
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, args, **kw: dispatched.append((name, args)) or '{"success": true}')
    server, names = qoder_adapter._workspace_mcp(FakeSDK, tmp_path, "turn-1", "session-1")
    read = next(tool for tool in server.tools if tool.name == "read_file")
    assert len(inspect.signature(read.handler).parameters) == 1
    arguments = {"path": "file.txt"}

    result = asyncio.run(read.handler(arguments))

    assert names == [
        "mcp__hermes_workspace__read_file", "mcp__hermes_workspace__search_files",
    ]
    assert result["content"][0]["text"] == '{"success": true}'
    assert len(dispatched) == 1
    assert dispatched[0][1]["path"] == str(tmp_path / "file.txt")


def test_qoder_workspace_edit_approval_and_replay(monkeypatch, tmp_path):
    class FakeSDK:
        @staticmethod
        def tool(name, description, schema):
            return lambda handler: SimpleNamespace(name=name, handler=handler)

        @staticmethod
        def create_sdk_mcp_server(name, tools):
            return SimpleNamespace(name=name, tools=tools)

    approvals = []
    dispatched = []
    monkeypatch.setattr(
        "tools.approval.request_explicit_approval",
        lambda **kwargs: approvals.append(kwargs) or True,
    )
    monkeypatch.setattr(
        "model_tools.handle_function_call",
        lambda name, args, **kwargs: dispatched.append((name, args)) or '{"success": true}',
    )
    server, _ = qoder_adapter._workspace_mcp(
        FakeSDK, tmp_path, "turn-edit", "session-1", allow_write=True,
    )
    write = next(tool for tool in server.tools if tool.name == "write_file")
    arguments = {"path": "file.txt", "content": "changed"}

    first = asyncio.run(write.handler(arguments))
    replay = asyncio.run(write.handler(arguments))

    assert first == replay
    assert len(approvals) == 1
    assert len(dispatched) == 1
    assert dispatched[0][1]["path"] == str(tmp_path / "file.txt")


def test_qoder_workspace_denied_edit_never_dispatches(monkeypatch, tmp_path):
    class FakeSDK:
        @staticmethod
        def tool(name, description, schema):
            return lambda handler: SimpleNamespace(name=name, handler=handler)

        @staticmethod
        def create_sdk_mcp_server(name, tools):
            return SimpleNamespace(name=name, tools=tools)

    dispatched = []
    monkeypatch.setattr("tools.approval.request_explicit_approval", lambda **kwargs: False)
    monkeypatch.setattr("model_tools.handle_function_call", lambda *args, **kwargs: dispatched.append(args))
    server, _ = qoder_adapter._workspace_mcp(
        FakeSDK, tmp_path, "turn-denied", "session-1", allow_write=True,
    )
    write = next(tool for tool in server.tools if tool.name == "write_file")

    denied = asyncio.run(write.handler({"path": "file.txt", "content": "changed"}))

    assert denied["is_error"]
    assert not dispatched


def test_qoder_cancellation_interrupts_and_disconnects(qoder):
    clients, output = qoder
    output[1:] = [None]
    with pytest.raises(InterruptedError):
        qoder_adapter.request([], "qoder-model", "test-token", lambda text: None, lambda: True)
    assert clients[0].interrupted
    assert clients[0].disconnected


def test_qoder_failure_after_output_retains_partial_text(qoder):
    clients, output = qoder
    output[-1] = ResultMessage(subtype="error_during_execution", is_error=True, error_code=10500)
    with pytest.raises(qoder_adapter.QoderError) as error:
        qoder_adapter.request([], "qoder-model", "test-token", lambda text: None, lambda: False)
    assert error.value.status_code == 503
    assert error.value.partial_text == "Hello"
    assert clients[0].disconnected


def test_qoder_allows_explicit_workspace_tool_block(qoder):
    _, output = qoder
    tool_name = "mcp__hermes_workspace__read_file"
    output.insert(1, AssistantMessage(
        model="qoder-model", usage={}, message_id=None, uuid=None,
        content=[ToolUseBlock(name=tool_name)],
    ))
    dispatched = set()

    class Client:
        async def receive_response(self):
            for item in output:
                yield item

    response = asyncio.run(qoder_adapter._receive(
        qoder_adapter._sdk(), Client(), "qoder-model", [], lambda text: None,
        allowed_tools={tool_name}, dispatched_tools=dispatched,
    ))
    assert response.choices[0].message.content == "Hello"
    assert dispatched == {tool_name}


def test_agent_qoder_turn_and_saved_history(qoder, monkeypatch):
    from tests.run_agent.test_provider_parity import _make_agent

    clients, _ = qoder
    agent = _make_agent(monkeypatch, "qoder")
    agent.model = qoder_adapter.DEFAULT_MODEL
    deltas = []
    first = agent.run_conversation("First", stream_callback=deltas.append)
    assert first["completed"], first
    assert first["final_response"] == "Hello"
    assert first["model"] == "qoder-model"
    assert first["qoder_credits"] == 0.2
    assert "Hello" in deltas
    second = agent.run_conversation("Follow-up", conversation_history=first["messages"])
    assert second["completed"], second
    assert "First" in clients[-1].prompt
    assert "Hello" in clients[-1].prompt
    assert "Follow-up" in clients[-1].prompt
    attachment = [{"type": "image_url", "image_url": {"url": "https://example.com/photo.png"}}]
    assert not agent.run_conversation(attachment)["completed"]


def test_qoder_missing_optional_sdk(qoder, monkeypatch):
    monkeypatch.setitem(sys.modules, "qoder_agent_sdk", None)
    with pytest.raises(qoder_adapter.QoderError, match="Install the Qoder SDK"):
        qoder_adapter.credentials()


def test_qoder_post_delta_error_does_not_replay(qoder, monkeypatch):
    from tests.run_agent.test_provider_parity import _make_agent

    clients, output = qoder
    output[-1] = ResultMessage(subtype="error_during_execution", is_error=True, error_code=10500)
    agent = _make_agent(monkeypatch, "qoder")
    result = agent.run_conversation("Question", stream_callback=lambda text: None)
    assert result["partial"]
    assert not result["completed"]
    assert result["final_response"] == "Hello"
    assert len(clients) == 1
    assert clients[0].disconnected


def test_qoder_post_tool_error_does_not_replay(qoder, monkeypatch):
    from tests.run_agent.test_provider_parity import _make_agent

    calls = []

    def fail_after_tool(**kwargs):
        calls.append(kwargs)
        raise qoder_adapter.QoderError("tool follow-up failed", tool_dispatched=True)

    monkeypatch.setattr(qoder_adapter, "request", fail_after_tool)
    agent = _make_agent(monkeypatch, "qoder")
    result = agent.run_conversation("Question")

    assert not result["completed"]
    assert result["error"] == "tool follow-up failed"
    assert len(calls) == 1


def test_switch_from_qoder_restores_hermes_tools(qoder, monkeypatch):
    from tests.run_agent.test_provider_parity import _make_agent

    messages = [{"role": "user", "content": "Question"}]
    agent = _make_agent(monkeypatch, "qoder")
    assert "tools" not in agent._build_api_kwargs(messages)
    agent.switch_model(
        new_model="other", new_provider="openrouter", api_key="test",
        base_url="https://openrouter.ai/api/v1", api_mode="chat_completions",
    )
    assert agent._build_api_kwargs(messages)["tools"]


def test_qoder_setup_persists_validated_model(qoder, monkeypatch):
    from hermes_cli import config, main

    config.save_config({"model": {
        "provider": "openrouter", "default": "old", "base_url": "https://example.com",
    }})

    def choose(labels, *, default=0):
        return next(
            index for index, label in enumerate(labels)
            if label.startswith(("More providers", "Qoder (", "qoder-model"))
        )

    monkeypatch.setattr(main, "_prompt_provider_choice", choose)
    main.select_provider_and_model()
    assert config.read_raw_config()["model"] == {"provider": "qoder", "default": "qoder-model"}


def test_qoder_pre_delta_failure_can_fall_back(qoder, monkeypatch):
    from tests.run_agent.test_provider_parity import _make_agent

    clients, output = qoder
    output[:] = [ResultMessage(subtype="error_during_execution", is_error=True, error_code=10605)]
    agent = _make_agent(monkeypatch, "qoder")
    agent._fallback_chain = [{"provider": "openrouter", "model": "fallback"}]
    fallback_client = SimpleNamespace(base_url="https://openrouter.ai/api/v1", api_key="fallback-key")
    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *a, **kw: (fallback_client, "fallback"))
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", lambda *a, **kw: SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content="Fallback answer", tool_calls=None),
        )],
        usage=None,
    ))

    result = agent.run_conversation("Question")
    assert result["completed"], result
    assert result["final_response"] == "Fallback answer"
    assert result["provider"] == "openrouter"
    assert len(clients) == 1
    assert clients[0].disconnected
