"""Tests for profile-scoped gateway custom commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.custom_commands import (
    CustomCommandRegistry,
    get_active_custom_commands,
    set_active_custom_commands,
    validate_custom_commands,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.session import SessionSource, build_session_key


@pytest.fixture(autouse=True)
def _reset_active_registry():
    previous = get_active_custom_commands()
    yield
    set_active_custom_commands(previous)


def _validate(raw, skills=None):
    skills = skills or {}

    def resolve(name):
        key = f"/{name.replace('_', '-')}"
        return key if key in skills else None

    with (
        patch("agent.skill_commands.get_skill_commands", return_value=skills),
        patch("agent.skill_commands.resolve_skill_command_key", side_effect=resolve),
    ):
        return validate_custom_commands(raw)


def test_valid_alias_skill_prompt_and_disabled_entry():
    registry, errors = _validate(
        {
            "s": {"type": "alias", "target": "status", "description": "Status"},
            "wb": {"type": "skill", "target": "wallet-balance", "description": "Balance"},
            "brief": {"type": "prompt", "text": "Summarize markets", "description": "Brief"},
            "off": {"type": "prompt", "text": "No", "description": "Off", "enabled": False},
        },
        skills={"/wallet-balance": {"name": "wallet-balance"}},
    )

    assert errors == []
    assert registry.get("s").target == "status"
    assert registry.get("wb").skill_key == "/wallet-balance"
    assert registry.get("brief").text == "Summarize markets"
    assert registry.get("off") is None


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"status": {"type": "prompt", "text": "x", "description": "x"}}, "collides"),
        ({"bad name": {"type": "prompt", "text": "x", "description": "x"}}, "invalid command name"),
        ({"x": {"type": "unknown", "description": "x"}}, "must be one of"),
        ({"x": {"type": "skill", "target": "missing", "description": "x"}}, "not installed"),
    ],
)
def test_invalid_entries_are_disabled(raw, message):
    registry, errors = _validate(raw)
    assert registry.values() == ()
    assert any(message in error for error in errors)


def test_alias_cycles_and_unknown_targets_are_rejected():
    registry, errors = _validate(
        {
            "a": {"type": "alias", "target": "b", "description": "A"},
            "b": {"type": "alias", "target": "a", "description": "B"},
            "c": {"type": "alias", "target": "missing", "description": "C"},
        }
    )
    assert registry.values() == ()
    assert sum("cycle" in error for error in errors) == 2
    assert any("unknown alias target" in error for error in errors)


def test_alias_chain_resolves_to_native_and_busy_bypass():
    registry, errors = _validate(
        {
            "fresh": {"type": "alias", "target": "n", "description": "Fresh"},
            "n": {"type": "alias", "target": "new", "description": "New"},
        }
    )
    assert errors == []
    assert registry.get("fresh").target == "new"
    assert registry.should_bypass_active_guard("fresh") is True


def test_telegram_menu_places_custom_commands_after_native():
    from hermes_cli.commands import telegram_bot_commands, telegram_menu_commands

    registry, errors = _validate(
        {"wb": {"type": "skill", "target": "wallet-balance", "description": "Check balance"}},
        skills={"/wallet-balance": {"name": "wallet-balance"}},
    )
    assert errors == []
    set_active_custom_commands(registry)

    menu, _ = telegram_menu_commands(max_commands=100)
    native_count = len(telegram_bot_commands())
    assert menu[native_count] == ("wb", "Check balance")


def test_telegram_menu_reports_custom_commands_omitted_by_cap():
    from hermes_cli.commands import telegram_bot_commands, telegram_menu_commands

    registry, errors = _validate(
        {
            "one": {"type": "prompt", "text": "one", "description": "One"},
            "two": {"type": "prompt", "text": "two", "description": "Two"},
        }
    )
    assert errors == []
    set_active_custom_commands(registry)
    native_count = len(telegram_bot_commands())

    menu, hidden = telegram_menu_commands(max_commands=native_count + 1)

    assert len(menu) == native_count + 1
    assert menu[-1][0] == "one"
    assert hidden >= 1


def test_exec_is_valid_but_hidden_from_help_and_menu():
    registry, errors = _validate(
        {"legacy": {"type": "exec", "command": "echo ok", "description": "Legacy"}}
    )
    assert errors == []
    assert registry.get("legacy").type == "exec"
    assert registry.help_lines() == []
    assert registry.telegram_entries() == []


def _runner_for_dispatch(registry):
    from gateway.hooks import HookRegistry
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._custom_commands = registry
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._update_prompt_pending = {}
    runner.hooks = HookRegistry()
    runner.pairing_store = MagicMock()
    runner.session_store = MagicMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._session_key_for_source = MagicMock(return_value="telegram:dm:chat")
    return runner


def _event(text):
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat",
            user_id="user",
            chat_type="dm",
        ),
    )


@pytest.mark.asyncio
async def test_alias_dispatches_to_native_handler_with_arguments_preserved():
    registry, errors = _validate(
        {"s": {"type": "alias", "target": "status", "description": "Status"}}
    )
    assert errors == []
    runner = _runner_for_dispatch(registry)
    runner._handle_status_command = AsyncMock(return_value="native status")

    result = await runner._handle_message(_event("/s detail"))

    assert result == "native status"
    called_event = runner._handle_status_command.await_args.args[0]
    assert called_event.text == "/status detail"


@pytest.mark.asyncio
async def test_skill_shortcut_forwards_arguments_to_normal_agent_flow():
    skills = {"/wallet-balance": {"name": "wallet-balance"}}
    registry, errors = _validate(
        {"wb": {"type": "skill", "target": "wallet-balance", "description": "Balance"}},
        skills=skills,
    )
    assert errors == []
    runner = _runner_for_dispatch(registry)

    async def capture(event, source, session_key):
        return event.text

    runner._handle_message_with_agent = capture
    with (
        patch("agent.skill_commands.get_skill_commands", return_value=skills),
        patch("agent.skill_commands.build_skill_invocation_message", return_value="LOADED SKILL: solana") as build,
        patch("agent.skill_utils.get_disabled_skill_names", return_value=set()),
    ):
        result = await runner._handle_message(_event("/wb solana"))

    assert result == "LOADED SKILL: solana"
    build.assert_called_once_with(
        "/wallet-balance", "solana", task_id="telegram:dm:chat"
    )


@pytest.mark.asyncio
async def test_prompt_shortcut_appends_user_arguments_and_uses_agent_flow():
    registry, errors = _validate(
        {"brief": {"type": "prompt", "text": "Summarize markets", "description": "Brief"}}
    )
    assert errors == []
    runner = _runner_for_dispatch(registry)

    async def capture(event, source, session_key):
        return event.text

    runner._handle_message_with_agent = capture
    result = await runner._handle_message(_event("/brief last 24 hours"))

    assert result == "Summarize markets\n\nUser-provided arguments:\nlast 24 hours"


@pytest.mark.asyncio
async def test_native_alias_bypasses_adapter_active_session_guard():
    class StubAdapter(BasePlatformAdapter):
        async def connect(self):
            return True

        async def disconnect(self):
            return None

        async def send(self, chat_id, text=None, **kwargs):
            return None

        async def get_chat_info(self, chat_id):
            return {}

    registry, errors = _validate(
        {"n": {"type": "alias", "target": "new", "description": "New"}}
    )
    assert errors == []
    adapter = StubAdapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter._custom_command_bypass = registry.should_bypass_active_guard
    adapter._message_handler = AsyncMock(return_value="reset")
    adapter._send_with_retry = AsyncMock()
    event = _event("/n")
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = MagicMock()

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    assert session_key not in adapter._pending_messages
