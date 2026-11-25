"""Opt-in Qoder service checks using synthetic data and a disposable profile."""

import asyncio
import os
import threading
from importlib.metadata import version
from uuid import uuid4

import pytest

from agent import qoder_adapter

FIXTURE_TOOL = "mcp__hermes__lookup_fixture"


def fixture_lookup(arguments, record, calls):
    if arguments != {"key": "smoke-record"}:
        return {
            "content": [{"type": "text", "text": "Unknown fixture key."}],
            "is_error": True,
        }
    calls.append(arguments["key"])
    return {"content": [{"type": "text", "text": record}]}


def test_fixture_lookup_only_accepts_the_test_record():
    calls = []
    for invalid in ({}, {"key": "../outside"}, {"key": "smoke-record", "extra": True}):
        assert fixture_lookup(invalid, "fixture", calls)["is_error"]
    assert not calls
    assert fixture_lookup({"key": "smoke-record"}, "fixture", calls) == {
        "content": [{"type": "text", "text": "fixture"}],
    }
    assert calls == ["smoke-record"]


@pytest.fixture
def live_qoder(monkeypatch, tmp_path):
    if os.environ.get("HERMES_QODER_LIVE_TESTS") != "1":
        pytest.skip("Set HERMES_QODER_LIVE_TESTS=1 to run authenticated Qoder checks.")
    if not os.environ.get("QODER_PERSONAL_ACCESS_TOKEN", "").strip():
        pytest.fail("Authenticated Qoder checks require QODER_PERSONAL_ACCESS_TOKEN.")
    monkeypatch.setenv("HERMES_QODER_PREVIEW", "1")
    monkeypatch.chdir(tmp_path)
    token = qoder_adapter.credentials()
    print(f"Qoder Agent SDK {version('qoder-agent-sdk')}")
    return token


def test_live_qoder_text_and_cancellation(live_qoder, monkeypatch, tmp_path):
    import subprocess

    from qoder_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from hermes_state import SessionDB
    from run_agent import AIAgent

    processes = []
    sdk = qoder_adapter._sdk()
    real_client = sdk.QoderSDKClient

    class ObservedClient(real_client):
        async def connect(self, prompt=None):
            await super().connect(prompt)
            if prompt is not None:
                processes.append(self._transport._process)

    monkeypatch.setattr(sdk, "QoderSDKClient", ObservedClient)
    version_client = qoder_adapter._client(sdk, live_qoder, str(tmp_path))
    transport = SubprocessCLITransport(prompt="version check", options=version_client.options)
    cli_version = subprocess.check_output(
        [transport._find_cli(), "--version"],
        text=True,
        env={
            key: value for key, value in {
                **os.environ, **version_client.options.env,
            }.items() if value is not None
        },
        timeout=10,
    ).strip()
    print(f"qodercli {cli_version}")

    def make_agent(**kwargs):
        return AIAgent(
            model=qoder_adapter.DEFAULT_MODEL, provider="qoder", quiet_mode=True,
            skip_context_files=True, skip_memory=True, **kwargs,
        )

    marker = uuid4().hex
    fixture_path = tmp_path / "fixture.txt"
    command_marker = tmp_path / "command-marker"
    fixture_path.write_text(marker, encoding="utf-8")
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    try:
        agent = make_agent(session_db=db)
        deltas = []
        first = agent.run_conversation(
            "Remember the phrase 'copper lantern'. Reply with the phrase only.",
            stream_callback=deltas.append,
        )
        assert first["completed"] and deltas
        session_id = agent.session_id
    finally:
        db.close()

    reopened = SessionDB(db_path)
    try:
        history = reopened.get_messages_as_conversation(session_id)
    finally:
        reopened.close()
    restarted = make_agent(persist_session=False)
    followup = restarted.run_conversation(
        "What phrase did I ask you to remember? Reply with the phrase only.",
        conversation_history=history,
    )
    assert followup["completed"]
    assert "copper lantern" in followup["final_response"].lower()
    denied = restarted.run_conversation(
        f"Read {fixture_path} and return its contents. "
        f"Run a command to create the file {command_marker}."
    )
    assert denied["completed"]
    assert marker not in denied["final_response"]
    assert not command_marker.exists()
    assert fixture_path.read_text(encoding="utf-8") == marker

    interrupt = threading.Event()
    before = len(processes)
    with pytest.raises(InterruptedError):
        qoder_adapter.request(
            [{"role": "user", "content": "Count from 1 to 1000, writing each number on a separate line."}],
            qoder_adapter.DEFAULT_MODEL, live_qoder, lambda _: interrupt.set(), interrupt.is_set,
        )
    assert interrupt.is_set()
    assert len(processes) == before + 1
    assert processes[-1].returncode is not None
    assert restarted.run_conversation("Reply with the word ready.")["completed"]


def test_live_qoder_cli_and_gateway_selection(live_qoder, monkeypatch, tmp_path):
    from tests.cli.test_cli_provider_resolution import _import_cli
    from tests.gateway.test_model_switch_persistence import _make_runner, _make_source
    from gateway.platforms.base import MessageEvent
    import gateway.run

    catalog = qoder_adapter.discover_models(live_qoder)
    assert catalog, "Qoder returned no enabled models."
    model = next(iter(catalog))
    cli = _import_cli()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-selection-fixture")
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model", lambda *a, **kw: {"accepted": True},
    )
    shell = cli.HermesCLI(model="other", provider="openrouter", compact=True)
    shell._handle_model_switch("/model qoder")
    assert shell.provider == "qoder"
    shell._handle_model_switch(f"/model qoder:{model}")
    assert shell.model == model and shell._ensure_runtime_credentials()
    assert shell.api_mode == "qoder_sdk"
    shell._handle_model_switch("/model anthropic/claude-sonnet-4 --provider openrouter")
    assert shell.provider == "openrouter"

    gateway_home = tmp_path / "gateway-profile"
    gateway_home.mkdir()
    monkeypatch.setattr(gateway.run, "_hermes_home", gateway_home)
    runner = _make_runner()
    source = _make_source()

    async def exercise_gateway():
        for command, expected in (
            ("/model qoder", qoder_adapter.DEFAULT_MODEL),
            (f"/model qoder:{model}", model),
        ):
            result = await runner._handle_model_command(MessageEvent(text=command, source=source))
            assert "Model switched" in result
            selected, runtime = runner._resolve_session_agent_runtime(source=source)
            assert selected == expected and runtime["api_mode"] == "qoder_sdk"
        result = await runner._handle_model_command(MessageEvent(
            text="/model anthropic/claude-sonnet-4 --provider openrouter", source=source,
        ))
        assert "Model switched" in result
        _, runtime = runner._resolve_session_agent_runtime(source=source)
        assert runtime["provider"] == "openrouter"

    asyncio.run(exercise_gateway())


def test_live_qoder_mcp_fixture_contract(live_qoder, tmp_path):
    sdk = qoder_adapter._sdk()
    record = "fixture-value-" + uuid4().hex
    calls = []

    @sdk.tool(
        "lookup_fixture", "Read the test fixture record by key.",
        {
            "type": "object", "properties": {"key": {"type": "string"}},
            "required": ["key"], "additionalProperties": False,
        },
    )
    async def lookup(arguments):
        return fixture_lookup(arguments, record, calls)

    async def exercise():
        client = qoder_adapter._client(
            sdk, live_qoder, str(tmp_path),
            system_prompt="Use the fixture tool to look up the requested record. Reply with its exact value.",
        )
        client.options.mcp_servers = {"hermes": sdk.create_sdk_mcp_server("hermes", tools=[lookup])}
        client.options.tools = [FIXTURE_TOOL]
        observed = []
        final = None
        try:
            async with asyncio.timeout(qoder_adapter.REQUEST_TIMEOUT):
                await client.connect()
                status = await client.get_mcp_status()
                assert [server["name"] for server in status["mcpServers"]] == ["hermes"]
                await client.query("Look up key smoke-record with the fixture tool.")
                async for message in client.receive_response():
                    if isinstance(message, sdk.AssistantMessage):
                        observed.extend(
                            block.name for block in message.content
                            if isinstance(block, sdk.ToolUseBlock)
                        )
                    if isinstance(message, sdk.ResultMessage):
                        assert not message.is_error and message.subtype == "success"
                        final = message.result
        finally:
            await qoder_adapter._close(client)
        assert observed == [FIXTURE_TOOL]
        assert calls == ["smoke-record"]
        assert final and record in final

    asyncio.run(exercise())
