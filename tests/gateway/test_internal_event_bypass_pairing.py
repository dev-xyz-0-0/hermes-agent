"""
Tests that internal synthetic events bypass authorization and do not trigger pairing.

Fully isolated version:
- No global state leakage
- Deterministic config + env
- Safe monkeypatch usage
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from unittest.mock import AsyncMock
import uuid

# ---------------------------------------------------------------------------
# Global Isolation Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Ensure no env leakage across tests."""
    for key in [
        "DISCORD_ALLOW_ALL_USERS",
        "DISCORD_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    """Isolate _hermes_home per test."""
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    return tmp_path


@pytest.fixture
def runner(hermes_home):
    """Create a fresh GatewayRunner."""
    return GatewayRunner(GatewayConfig())


@pytest.fixture
def discord_adapter():
    return SimpleNamespace(
        send=AsyncMock(),
        handle_message=AsyncMock()
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Return pre-canned sessions, then None once exhausted."""

    def __init__(self, sessions):
        self._sessions = list(sessions)

    def get(self, session_id):
        if self._sessions:
            return self._sessions.pop(0)
        return None


def watcher_dict():
    return {
        "session_id": "proc_test_internal",
        "check_interval": 0,
        "session_key": "agent:main:discord:dm:123",
        "platform": "discord",
        "chat_id": "123",
        "thread_id": "",
        "notify_on_complete": True,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_on_complete_sets_internal_flag(
    monkeypatch, runner, discord_adapter
):
    """Synthetic completion event must have internal=True."""
    import tools.process_registry as pr_module

    sessions = [
        SimpleNamespace(
            output_buffer="done\n",
            exited=True,
            exit_code=0,
            command="echo test",
        ),
    ]

    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        return None

    # Scoped patch (important)
    monkeypatch.setattr("gateway.run.asyncio.sleep", _instant_sleep)

    runner.adapters[Platform.DISCORD] = discord_adapter
    runner._session_model_overrides = {}
    await runner._run_process_watcher(watcher_dict())

    assert discord_adapter.handle_message.await_count == 1
    event = discord_adapter.handle_message.await_args.args[0]

    assert isinstance(event, MessageEvent)
    assert event.internal is True, "Synthetic completion event must be marked internal"


@pytest.mark.asyncio
async def test_internal_event_bypasses_authorization(monkeypatch, runner):
    """Internal event should skip authorization."""
    auth_called = False
    original_auth = GatewayRunner._is_user_authorized

    def tracking_auth(self, src):
        nonlocal auth_called
        auth_called = True
        return original_auth(self, src)

    monkeypatch.setattr(GatewayRunner, "_is_user_authorized", tracking_auth)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="dm",
    )

    event = MessageEvent(
        text="[SYSTEM: done]",
        source=source,
        internal=True,
    )

    try:
        await runner._handle_message(event)
    except Exception:
        pass  # Expected — downstream code needs more setup

    assert not auth_called, (
        "_is_user_authorized should NOT be called for internal events"
    )


@pytest.mark.asyncio
async def test_internal_event_does_not_trigger_pairing(
    monkeypatch, runner, discord_adapter
):
    """Internal event must not generate pairing."""
    runner.adapters[Platform.DISCORD] = discord_adapter

    generate_called = False

    def tracking_generate(*args, **kwargs):
        nonlocal generate_called
        generate_called = True
        return "dummy"

    monkeypatch.setattr(
        runner.pairing_store,
        "generate_code",
        tracking_generate,
    )

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="dm",  # DM would normally trigger pairing
    )
    event = MessageEvent(
        text="[SYSTEM: Background process completed]",
        source=source,
        internal=True,
    )

    try:
        await runner._handle_message(event)
    except Exception:
        pass  # Expected — downstream code needs more setup

    assert not generate_called, (
        "Pairing code should NOT be generated for internal events"
    )

@pytest.mark.asyncio
async def test_non_internal_event_triggers_pairing(
    monkeypatch, runner, discord_adapter
):
    """Normal event should trigger pairing."""
    runner.adapters[Platform.DISCORD] = discord_adapter

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="dm",
        user_id=f"test_user_{uuid.uuid4()}",
    )
    # Normal event (not internal)
    event = MessageEvent(
        text="hello",
        source=source,
        internal=False,
    )

    result = await runner._handle_message(event)

    # Should return None (unauthorized) and send pairing message
    assert result is None
    assert discord_adapter.send.await_count == 1

    sent_text = discord_adapter.send.await_args.args[1]
    assert "don't recognize you" in sent_text
    
    
# This test suite verifies a **specific regression scenario in your GatewayRunner message pipeline**:

# > Internal/system-generated events must **bypass user authorization** and **must NOT trigger pairing logic**, even if they lack a `user_id`.

# ---

# ## Core concept being tested

# Your system has two types of events:

# ### 1. External (user) events

# * Origin: Discord/Telegram user
# * Require:

#   * Authorization check (`_is_user_authorized`)
#   * Pairing if unknown user

# ### 2. Internal (synthetic/system) events

# * Origin: background processes (e.g. `_run_process_watcher`)
# * Properties:

#   * `internal=True`
#   * Often **no `user_id`**
# * Must:

#   * Skip authorization
#   * Skip pairing
#   * Still be processed normally

# ---

# ## What each test validates

# ---

# ### 1) `test_notify_on_complete_sets_internal_flag`

# **Purpose**
# Ensure that background process completion generates an event marked as internal.

# **What happens**

# * Simulates a completed process via fake registry
# * Runs `_run_process_watcher()`
# * Captures emitted event

# **Assertion**

# ```python
# assert event.internal is True
# ```

# **Why this matters**
# If `internal=True` is missing:

# * Event gets treated as user input
# * Leads to auth + pairing logic being triggered incorrectly

# ---

# ### 2) `test_internal_event_bypasses_authorization`

# **Purpose**
# Ensure internal events do NOT call `_is_user_authorized`.

# **What happens**

# * Creates an event:

#   * `internal=True`
#   * no `user_id`
# * Monkeypatches `_is_user_authorized` to track calls
# * Calls `_handle_message`

# **Assertion**

# ```python
# assert not auth_called
# ```

# **Why this matters**
# If authorization is triggered:

# * Event gets rejected
# * System-generated messages break
# * Background workflows fail silently

# ---

# ### 3) `test_internal_event_does_not_trigger_pairing`

# **Purpose**
# Ensure internal events do NOT generate pairing codes.

# **What happens**

# * Creates internal event with no `user_id`
# * Hooks into `pairing_store.generate_code`
# * Runs `_handle_message`

# **Assertion**

# ```python
# assert not generate_called
# ```

# **Why this matters**
# This is the **actual regression bug**:

# Without this safeguard:

# * System event → treated as unknown user
# * Gateway sends pairing code to chat
# * Results in:

#   * Spam
#   * Confusing UX
#   * Security concerns

# ---

# ### 4) `test_non_internal_event_triggers_pairing`

# **Purpose**
# Ensure normal behavior still works (control test).

# **What happens**

# * Creates:

#   * `internal=False`
#   * unknown `user_id`
# * Runs `_handle_message`

# **Assertions**

# ```python
# assert result is None
# assert adapter.send.await_count == 1
# ```

# **Why this matters**
# Confirms you didn’t break the real flow while fixing the bug.

# ---

# ## What bug this suite prevents

# Before fix:

# ```
# _process_watcher →
#   emits MessageEvent (no user_id) →
#     _handle_message →
#       _is_user_authorized → FAIL →
#         triggers pairing →
#           sends pairing code to Discord
# ```

# After fix:

# ```
# _process_watcher →
#   emits MessageEvent (internal=True) →
#     _handle_message →
#       bypass auth →
#       bypass pairing →
#       processed correctly
# ```

# ---

# ## What layer is being tested

# This is a **behavioral integration test of the message pipeline**, specifically:

# ```text
# _process_watcher
#     ↓
# MessageEvent(internal=True)
#     ↓
# _handle_message
#     ├── authorization gate
#     ├── pairing logic
#     └── adapter routing
# ```

# ---

# ## Key invariant enforced by this test suite

# ```text
# IF event.internal == True:
#     skip authorization
#     skip pairing
# ```

# ---

# ## Why this is critical in your system

# Given your Hermes + Gateway architecture:

# * Internal events = agent actions, background jobs, cron tasks
# * These must behave like **trusted system messages**

# If broken:

# * Agents stop working
# * Background workflows fail
# * Chat gets polluted with pairing prompts
# * Security model becomes inconsistent

# ---

# ## Summary

# This test suite ensures:

# 1. Internal events are correctly marked
# 2. Authorization is skipped for internal events
# 3. Pairing is NOT triggered for internal events
# 4. Normal user flow remains unchanged

# It protects a **core contract in your gateway:**

# > System-generated events must never be treated as untrusted user input.
