"""Tests for generic one-shot approvals used by MCP trust gating."""

import os
import threading
import time
from unittest.mock import MagicMock, patch


def _wait_for_pending(approval, session_key: str, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if approval.pending_approval_count(session_key) > 0:
            return True
        time.sleep(0.01)
    return False


def test_explicit_gateway_approval_once_returns_true():
    from tools import approval

    session_key = "telegram:test-once"
    notify = MagicMock()
    approval.register_gateway_notify(session_key, notify)
    result = {}

    try:
        os.environ["HERMES_SESSION_KEY"] = session_key
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "approved",
                approval.request_explicit_approval(
                    title="MCP approval: dev/multiply",
                    description="Multiply 3 and 9",
                    command="MCP dev.multiply",
                ),
            )
        )
        thread.start()
        assert _wait_for_pending(approval, session_key)
        assert approval.resolve_gateway_approval(session_key, "once") == 1
        thread.join(timeout=1)

        assert not thread.is_alive()
        assert result["approved"] is True
        notify.assert_called_once()
        payload = notify.call_args.args[0]
        assert payload["approval_type"] == "explicit"
        assert payload["allow_permanent"] is False
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)
        os.environ.pop("HERMES_SESSION_KEY", None)


def test_explicit_gateway_deny_returns_false():
    from tools import approval

    session_key = "telegram:test-deny"
    approval.register_gateway_notify(session_key, lambda data: None)
    result = {}

    try:
        os.environ["HERMES_SESSION_KEY"] = session_key
        os.environ["HERMES_SESSION_KEY"] = session_key
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "approved",
                approval.request_explicit_approval(
                    title="MCP approval: dev/multiply",
                    description="Multiply 3 and 9",
                    command="MCP dev.multiply",
                ),
            )
        )
        thread.start()
        assert _wait_for_pending(approval, session_key)
        assert approval.resolve_gateway_approval(session_key, "deny") == 1
        thread.join(timeout=1)
        assert result["approved"] is False
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)
        os.environ.pop("HERMES_SESSION_KEY", None)


def test_explicit_gateway_session_choice_is_not_persistent_approval():
    from tools import approval

    session_key = "telegram:test-session"
    approval.register_gateway_notify(session_key, lambda data: None)
    result = {}

    try:
        os.environ["HERMES_SESSION_KEY"] = session_key
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "approved",
                approval.request_explicit_approval(
                    title="MCP approval: dev/multiply",
                    description="Multiply 3 and 9",
                    command="MCP dev.multiply",
                ),
            )
        )
        thread.start()
        assert _wait_for_pending(approval, session_key)
        assert approval.resolve_gateway_approval(session_key, "session") == 1
        thread.join(timeout=1)
        assert result["approved"] is False
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)
        os.environ.pop("HERMES_SESSION_KEY", None)


def test_explicit_approval_notify_failure_fails_closed():
    from tools import approval

    session_key = "telegram:test-notify-error"

    def fail(_data):
        raise RuntimeError("send failed")

    approval.register_gateway_notify(session_key, fail)
    try:
        os.environ["HERMES_SESSION_KEY"] = session_key
        assert approval.request_explicit_approval(
            title="MCP approval: dev/multiply",
            description="Multiply 3 and 9",
            command="MCP dev.multiply",
        ) is False
        assert approval.pending_approval_count(session_key) == 0
    finally:
        approval.unregister_gateway_notify(session_key)
        approval.clear_session(session_key)
        os.environ.pop("HERMES_SESSION_KEY", None)


def test_explicit_approval_without_channel_fails_closed():
    from tools import approval

    session_key = "no-channel"
    try:
        os.environ["HERMES_SESSION_KEY"] = session_key
        with patch.dict("os.environ", {}, clear=False):
            # Ensure no CLI fallback is active for this test.
            old = os.environ.pop("HERMES_INTERACTIVE", None)
            try:
                assert approval.request_explicit_approval(
                    title="MCP approval: dev/multiply",
                    description="Multiply 3 and 9",
                    command="MCP dev.multiply",
                ) is False
            finally:
                if old is not None:
                    os.environ["HERMES_INTERACTIVE"] = old
    finally:
        approval.clear_session(session_key)
        os.environ.pop("HERMES_SESSION_KEY", None)
