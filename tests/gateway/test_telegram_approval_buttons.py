"""Tests for Telegram inline keyboard approval buttons."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    """Wire up the minimal mocks required to import TelegramAdapter."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    # Provide real exception classes so ``except (NetworkError, ...)`` in
    # connect() doesn't blow up under xdist when this mock leaks.
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter
from gateway.config import Platform, PlatformConfig


def _make_adapter():
    """Create a TelegramAdapter with mocked internals."""
    config = PlatformConfig(enabled=True, token="test-token")
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


# ===========================================================================
# send_exec_approval — inline keyboard buttons
# ===========================================================================

class TestTelegramExecApproval:
    """Test the send_exec_approval method sends InlineKeyboard buttons."""

    @pytest.mark.asyncio
    async def test_sends_inline_keyboard(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="rm -rf /important",
            session_key="agent:main:telegram:group:12345:99",
            description="dangerous deletion",
        )

        assert result.success is True
        assert result.message_id == "42"

        adapter._bot.send_message.assert_called_once()
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "rm -rf /important" in kwargs["text"]
        assert "dangerous deletion" in kwargs["text"]
        assert kwargs["reply_markup"] is not None  # InlineKeyboardMarkup

    @pytest.mark.asyncio
    async def test_stores_approval_state(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="echo test",
            session_key="my-session-key",
        )

        # The approval_id should map to the session_key
        assert len(adapter._approval_state) == 1
        approval_id = list(adapter._approval_state.keys())[0]
        assert adapter._approval_state[approval_id] == "my-session-key"

    @pytest.mark.asyncio
    async def test_sends_in_thread(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="ls",
            session_key="s",
            metadata={"thread_id": "999"},
        )

        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs.get("message_thread_id") == 999

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._bot = None
        result = await adapter.send_exec_approval(
            chat_id="12345", command="ls", session_key="s"
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_truncates_long_command(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        long_cmd = "x" * 5000
        await adapter.send_exec_approval(
            chat_id="12345", command=long_cmd, session_key="s"
        )

        kwargs = adapter._bot.send_message.call_args[1]
        assert "..." in kwargs["text"]
        assert len(kwargs["text"]) < 5000
        
    @pytest.mark.asyncio
    async def test_does_not_use_parse_mode(self):
        """Approval messages must be sent as plain text.

        Arbitrary MCP commands may contain Telegram Markdown-sensitive
        characters. Using Markdown/MarkdownV2 can cause Telegram to reject
        otherwise valid approval prompts.
        """
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="echo test",
            session_key="s",
            description="test command",
        )

        kwargs = adapter._bot.send_message.call_args[1]

        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_markdown_special_characters_are_sent_as_plain_text(self):
        """Commands containing Markdown-sensitive characters must not break."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        command = (
            "python script.py "
            "--token='abc_def' "
            "--value='*test*' "
            "--json='{\"key\":\"[value](url)\"}' "
            "--code='`dangerous`'"
        )

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command=command,
            session_key="s",
            description="MCP execution_test",
        )

        assert result.success is True

        kwargs = adapter._bot.send_message.call_args[1]

        # The command should appear unchanged.
        assert command in kwargs["text"]

        # Most importantly, Telegram should not be asked to parse it.
        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_backticks_do_not_break_approval_message(self):
        """Regression test for Telegram 'Can't parse entities' failures."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        command = "echo `hello` && echo ```test```"

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command=command,
            session_key="s",
        )

        assert result.success is True

        kwargs = adapter._bot.send_message.call_args[1]

        assert command in kwargs["text"]
        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_mcp_json_arguments_do_not_break_approval_message(self):
        """Realistic MCP tool arguments should be safe in Telegram prompts."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        command = (
            'mcp_dev_okx_solana_execute_swap '
            '{"token_address":"HUcEu35oz1yku1yLBSYRL1UyAnAh59ZCkAYPRe2xpump",'
            '"amount":"1000000",'
            '"slippage":"0.5%"}'
        )

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command=command,
            session_key="agent:main:telegram:12345",
            description="Execute MCP trading tool",
        )

        assert result.success is True

        kwargs = adapter._bot.send_message.call_args[1]

        assert command in kwargs["text"]
        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_description_with_markdown_characters_is_safe(self):
        """Descriptions can also contain Telegram Markdown characters."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        description = (
            "Execute *dangerous* tool for user_name "
            "[approval] with `arguments`"
        )

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="echo test",
            session_key="s",
            description=description,
        )

        assert result.success is True

        kwargs = adapter._bot.send_message.call_args[1]

        assert description in kwargs["text"]
        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_unicode_and_emoji_are_preserved(self):
        """Unicode should survive approval-message construction unchanged."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        command = "echo '测试 🚀 日本語 한글'"

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command=command,
            session_key="s",
        )

        assert result.success is True

        kwargs = adapter._bot.send_message.call_args[1]

        assert command in kwargs["text"]
        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_long_command_is_truncated_to_expected_preview_length(self):
        """Command preview should be bounded before sending to Telegram."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 1
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        long_cmd = "x" * 5000

        await adapter.send_exec_approval(
            chat_id="12345",
            command=long_cmd,
            session_key="s",
        )

        kwargs = adapter._bot.send_message.call_args[1]
        text = kwargs["text"]

        # New implementation truncates the command itself at 3500 chars.
        assert ("x" * 3500) in text
        assert ("x" * 3501) not in text
        assert "..." in text

        # Still plain text.
        assert "parse_mode" not in kwargs


    @pytest.mark.asyncio
    async def test_send_message_failure_returns_error(self):
        """Telegram API failures should return SendResult(success=False)."""
        adapter = _make_adapter()

        adapter._bot.send_message = AsyncMock(
            side_effect=RuntimeError("Telegram API unavailable")
        )

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="echo test",
            session_key="s",
        )

        assert result.success is False
        assert "Telegram API unavailable" in result.error


    @pytest.mark.asyncio
    async def test_failed_send_does_not_store_approval_state(self):
        """Approval state must only be stored after Telegram accepts message."""
        adapter = _make_adapter()

        adapter._bot.send_message = AsyncMock(
            side_effect=RuntimeError("send failed")
        )

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="echo test",
            session_key="secret-session-key",
        )

        assert result.success is False
        assert adapter._approval_state == {}


    @pytest.mark.asyncio
    async def test_message_thread_id_fallback(self):
        """message_thread_id metadata should work when thread_id is absent."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="ls",
            session_key="s",
            metadata={"message_thread_id": "777"},
        )

        kwargs = adapter._bot.send_message.call_args[1]

        assert kwargs["message_thread_id"] == 777


    @pytest.mark.asyncio
    async def test_thread_id_takes_precedence_over_message_thread_id(self):
        """thread_id should take precedence when both metadata values exist."""
        adapter = _make_adapter()

        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="ls",
            session_key="s",
            metadata={
                "thread_id": "111",
                "message_thread_id": "222",
            },
        )

        kwargs = adapter._bot.send_message.call_args[1]

        assert kwargs["message_thread_id"] == 111


# ===========================================================================
# _handle_callback_query — approval button clicks
# ===========================================================================

class TestTelegramApprovalCallback:
    """Test the approval callback handling in _handle_callback_query."""

    @pytest.mark.asyncio
    async def test_resolves_approval_on_click(self):
        adapter = _make_adapter()
        # Set up approval state
        adapter._approval_state[1] = "agent:main:telegram:group:12345:99"

        # Mock callback query
        query = AsyncMock()
        query.data = "ea:once:1"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Norbert"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._handle_callback_query(update, context)

        mock_resolve.assert_called_once_with("agent:main:telegram:group:12345:99", "once")
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

        # State should be cleaned up
        assert 1 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_deny_button(self):
        adapter = _make_adapter()
        adapter._approval_state[2] = "some-session"

        query = AsyncMock()
        query.data = "ea:deny:2"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Alice"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._handle_callback_query(update, context)

        mock_resolve.assert_called_once_with("some-session", "deny")
        edit_kwargs = query.edit_message_text.call_args[1]
        assert "Denied" in edit_kwargs["text"]

    @pytest.mark.asyncio
    async def test_already_resolved(self):
        adapter = _make_adapter()
        # No state for approval_id 99 — already resolved

        query = AsyncMock()
        query.data = "ea:once:99"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Bob"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._handle_callback_query(update, context)

        # Should NOT resolve — already handled
        mock_resolve.assert_not_called()
        # Should still ack with "already resolved" message
        query.answer.assert_called_once()
        assert "already been resolved" in query.answer.call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_model_picker_callback_not_affected(self):
        """Ensure model picker callbacks still route correctly."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "mp:some_provider"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        # Model picker callback should be handled (not crash)
        # We just verify it doesn't try to resolve an approval
        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            with patch.object(adapter, "_handle_model_picker_callback", new_callable=AsyncMock):
                await adapter._handle_callback_query(update, context)

        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_not_affected(self, tmp_path):
        """Ensure update prompt callbacks still work."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 123
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
           with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
                with patch.dict(os.environ, {"TELEGRAM_APPROVAL_USERS": ""}):
                    await adapter._handle_callback_query(update, context)
        # Should NOT have triggered approval resolution
        mock_resolve.assert_not_called()
        assert (tmp_path / ".update_response").read_text() == "y"

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_unauthorized_user(self, tmp_path):
        """Update prompt buttons should honor TELEGRAM_APPROVAL_USERS."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_APPROVAL_USERS": "111"}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_allows_authorized_user(self, tmp_path):
        """Allowed Telegram users can still answer update prompt buttons."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:n"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 111
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_APPROVAL_USERS": "111"}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()
        assert (tmp_path / ".update_response").read_text() == "n"
