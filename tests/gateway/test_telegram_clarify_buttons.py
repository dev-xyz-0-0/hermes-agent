"""Telegram clarify button rendering and callback tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.telegram import TelegramAdapter
from gateway.platforms import telegram as telegram_module
from tests.gateway.test_telegram_approval_buttons import _make_adapter
from tools.clarify_gateway import clear_session, pending, register
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _Button:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class _Keyboard:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


@pytest.fixture(autouse=True)
def _telegram_keyboard_types(monkeypatch):
    # The approval test module installs a lightweight telegram module mock
    # whose classes resolve to typing.Any. These concrete test doubles let
    # clarify keyboard construction run under xdist and full-suite imports.
    monkeypatch.setattr(telegram_module, "InlineKeyboardButton", _Button)
    monkeypatch.setattr(telegram_module, "InlineKeyboardMarkup", _Keyboard)


@pytest.mark.asyncio
async def test_send_clarify_builds_choice_and_other_buttons():
    adapter = _make_adapter()
    message = MagicMock(message_id=77)
    adapter._bot.send_message = AsyncMock(return_value=message)
    req = register("telegram-clarify-send", "Choose", ["One", "Two"])
    result = await adapter.send_clarify(
        "123", "Choose", ["One", "Two"], req.request_id, metadata={"thread_id": "42"}
    )
    assert result.success
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["message_thread_id"] == 42
    keyboard = kwargs["reply_markup"]
    callback_data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert len(callback_data) == 3
    callback_id = int(keyboard.inline_keyboard[-1][0].callback_data.split(":")[1])
    assert callback_data[-1] == f"cl:{callback_id}:o"
    assert all(len(data.encode()) <= 64 for data in callback_data)
    assert keyboard.inline_keyboard[0][0].text == "One"
    assert adapter._clarify_state[callback_id]["message_id"] == 77
    clear_session("telegram-clarify-send")


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Two", "1", "3"])
async def test_callback_resolves_choice_and_cleans_buttons(label):
    adapter = _make_adapter()
    req = register("telegram-clarify-choice", "Choose", ["One", label])
    adapter._clarify_state[req.request_id] = {"choices": ["One", label], "message_id": 1,
                                              "request_id": req.request_id}
    query = MagicMock()
    query.data = f"cl:{req.request_id}:1"
    query.from_user.id = 123
    query.from_user.first_name = "Tester"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock(callback_query=query)
    await adapter._handle_callback_query(update, MagicMock())
    assert req.response == label
    assert req.event.is_set()
    assert req.request_id not in adapter._clarify_state
    query.edit_message_text.assert_awaited_once()
    clear_session("telegram-clarify-choice")


@pytest.mark.asyncio
async def test_callback_rejects_unauthorized_user(monkeypatch):
    adapter = _make_adapter()
    req = register("telegram-clarify-unauthorized", "Choose", ["One"])
    adapter._clarify_state[req.request_id] = {"choices": ["One"], "message_id": 1,
                                              "request_id": req.request_id}
    monkeypatch.setenv("TELEGRAM_APPROVAL_USERS", "456")
    query = MagicMock()
    query.data = f"cl:{req.request_id}:0"
    query.from_user.id = 123
    query.answer = AsyncMock()
    await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
    assert not req.event.is_set()
    query.answer.assert_awaited_once()
    assert "not authorized" in query.answer.await_args.kwargs["text"]
    clear_session("telegram-clarify-unauthorized")


@pytest.mark.asyncio
async def test_callback_rejects_invalid_token_and_stale_request():
    adapter = _make_adapter()
    bad_query = MagicMock()
    bad_query.data = "cl:not-a-number:0"
    bad_query.answer = AsyncMock()
    await adapter._handle_callback_query(MagicMock(callback_query=bad_query), MagicMock())
    assert "Invalid" in bad_query.answer.await_args.kwargs["text"]

    stale_query = MagicMock()
    stale_query.data = "cl:999999:0"
    stale_query.from_user.id = 123
    stale_query.answer = AsyncMock()
    await adapter._handle_callback_query(MagicMock(callback_query=stale_query), MagicMock())
    assert "already been answered" in stale_query.answer.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_send_clarify_handles_disconnected_and_send_failure():
    adapter = _make_adapter()
    adapter._bot = None
    result = await adapter.send_clarify("123", "Choose", ["One"], 1)
    assert not result.success

    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(side_effect=RuntimeError("send failed"))
    result = await adapter.send_clarify("123", "Choose", ["One"], 2)
    assert not result.success
    assert "send failed" in result.error


@pytest.mark.asyncio
async def test_long_choice_labels_are_truncated_and_prompt_html_is_plain_text():
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=4))
    req = register("telegram-clarify-long", "<b>Question</b>", ["L" * 200])
    await adapter.send_clarify("123", req.question, req.choices, req.request_id)
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["text"] == "❓ <b>Question</b>"
    assert kwargs.get("parse_mode") is None
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert len(button.text) <= 60
    clear_session("telegram-clarify-long")


@pytest.mark.asyncio
async def test_other_callback_can_be_followed_by_text_resolution():
    adapter = _make_adapter()
    req = register("telegram-clarify-other-answer", "Choose", ["One"])
    adapter._clarify_state[req.request_id] = {"choices": ["One"], "message_id": 1,
                                              "request_id": req.request_id}
    query = MagicMock()
    query.data = f"cl:{req.request_id}:o"
    query.from_user.id = 123
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
    from tools.clarify_gateway import resolve
    assert resolve("telegram-clarify-other-answer", "custom")
    assert req.response == "custom"
    clear_session("telegram-clarify-other-answer")


@pytest.mark.asyncio
async def test_other_callback_switches_request_to_text_mode():
    adapter = _make_adapter()
    req = register("telegram-clarify-other", "Choose", ["One"])
    adapter._clarify_state[req.request_id] = {"choices": ["One"], "message_id": 1,
                                              "request_id": req.request_id}
    query = MagicMock()
    query.data = f"cl:{req.request_id}:o"
    query.from_user.id = 123
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
    assert pending("telegram-clarify-other").awaiting_text
    assert not req.event.is_set()
    clear_session("telegram-clarify-other")


@pytest.mark.asyncio
async def test_open_ended_prompt_uses_text_fallback():
    adapter = _make_adapter()
    adapter.send = AsyncMock(return_value=MagicMock(success=True))
    result = await adapter.send_clarify("123", "What should I do?", None, 99)
    assert result.success
    adapter.send.assert_awaited_once()
    assert "Reply with your answer" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_base_adapter_numbered_text_fallback():
    adapter = _make_adapter()
    adapter.send = AsyncMock(return_value=SendResult(success=True))
    result = await BasePlatformAdapter.send_clarify(
        adapter, "123", "Choose", ["One", "Two"], 10
    )
    assert result.success
    content = adapter.send.await_args.args[1]
    assert "1. One" in content and "2. Two" in content and "3. Other" in content
