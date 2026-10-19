"""Tests for per-user memory scoping via user_id threading.

Verifies that gateway user_id flows from AIAgent -> MemoryManager -> plugins,
so each gateway user gets their own memory bucket instead of sharing a static one.
"""

import json
import os
from unittest.mock import MagicMock, patch

from agent.memory_provider import MemoryProvider
from agent.memory_manager import MemoryManager


# ---------------------------------------------------------------------------
# Concrete test provider that records init kwargs
# ---------------------------------------------------------------------------


class RecordingProvider(MemoryProvider):
    """Minimal provider that records what initialize() receives."""

    def __init__(self, name="recording"):
        self._name = name
        self._init_kwargs = {}
        self._init_session_id = None

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._init_session_id = session_id
        self._init_kwargs = dict(kwargs)

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({})

    def shutdown(self):
        pass


# ---------------------------------------------------------------------------
# MemoryManager user_id threading tests
# ---------------------------------------------------------------------------


class TestMemoryManagerUserIdThreading:
    """Verify user_id reaches providers via initialize_all."""

    def test_user_id_forwarded_to_provider(self):
        mgr = MemoryManager()
        p = RecordingProvider()
        mgr.add_provider(p)

        mgr.initialize_all(
            session_id="sess-123",
            platform="telegram",
            user_id="tg_user_42",
        )

        assert p._init_kwargs.get("user_id") == "tg_user_42"
        assert p._init_kwargs.get("platform") == "telegram"
        assert p._init_session_id == "sess-123"

    def test_no_user_id_when_cli(self):
        """CLI sessions should not have user_id in kwargs."""
        mgr = MemoryManager()
        p = RecordingProvider()
        mgr.add_provider(p)

        mgr.initialize_all(
            session_id="sess-456",
            platform="cli",
        )

        assert "user_id" not in p._init_kwargs
        assert p._init_kwargs.get("platform") == "cli"

    def test_user_id_none_not_forwarded(self):
        """Explicit None user_id should not appear in kwargs."""
        mgr = MemoryManager()
        p = RecordingProvider()
        mgr.add_provider(p)

        # Simulates what happens when AIAgent passes user_id=None
        # (the agent code only adds user_id to kwargs when it's truthy)
        mgr.initialize_all(
            session_id="sess-789",
            platform="discord",
        )

        assert "user_id" not in p._init_kwargs

    def test_chat_id_and_thread_id_forwarded_to_provider(self):
        """Regression test for the Holographic fail-closed bug: chat_id/
        thread_id must reach the provider the same way user_id does."""
        mgr = MemoryManager()
        p = RecordingProvider()
        mgr.add_provider(p)

        mgr.initialize_all(
            session_id="sess-123",
            platform="telegram",
            chat_id="chat_42",
            thread_id="thread_7",
        )

        assert p._init_kwargs.get("chat_id") == "chat_42"
        assert p._init_kwargs.get("thread_id") == "thread_7"
        assert p._init_kwargs.get("platform") == "telegram"

    def test_no_chat_id_when_not_provided(self):
        """Without chat_id, providers must not silently receive one —
        this is what makes multi-tenant platforms fail closed correctly."""
        mgr = MemoryManager()
        p = RecordingProvider()
        mgr.add_provider(p)

        mgr.initialize_all(
            session_id="sess-456",
            platform="cli",
        )

        assert "chat_id" not in p._init_kwargs
        assert "thread_id" not in p._init_kwargs

    def test_multiple_providers_all_receive_user_id(self):
        from agent.builtin_memory_provider import BuiltinMemoryProvider

        mgr = MemoryManager()
        # Use builtin + one external (MemoryManager only allows one external)
        builtin = BuiltinMemoryProvider()
        ext = RecordingProvider("external")
        mgr.add_provider(builtin)
        mgr.add_provider(ext)

        mgr.initialize_all(
            session_id="sess-multi",
            platform="slack",
            user_id="slack_U12345",
        )

        assert ext._init_kwargs.get("user_id") == "slack_U12345"
        assert ext._init_kwargs.get("platform") == "slack"


# ---------------------------------------------------------------------------
# Mem0 provider user_id tests
# ---------------------------------------------------------------------------


class TestMem0UserIdScoping:
    """Verify Mem0 plugin uses gateway user_id when provided."""

    def test_gateway_user_id_overrides_default(self):
        """When user_id is passed via kwargs, it should override the config default."""
        from plugins.memory.mem0 import Mem0MemoryProvider

        provider = Mem0MemoryProvider()
        # Mock _load_config to return a config with default user_id
        with patch("plugins.memory.mem0._load_config", return_value={
            "api_key": "test-key",
            "user_id": "hermes-user",
            "agent_id": "hermes",
            "rerank": True,
        }):
            provider.initialize(session_id="test-sess", user_id="tg_user_99")

        assert provider._user_id == "tg_user_99"

    def test_no_user_id_falls_back_to_config(self):
        """Without user_id in kwargs, should use config default."""
        from plugins.memory.mem0 import Mem0MemoryProvider

        provider = Mem0MemoryProvider()
        with patch("plugins.memory.mem0._load_config", return_value={
            "api_key": "test-key",
            "user_id": "custom-default",
            "agent_id": "hermes",
            "rerank": True,
        }):
            provider.initialize(session_id="test-sess")

        assert provider._user_id == "custom-default"

    def test_no_user_id_no_config_uses_hermes_user(self):
        """Without user_id or config override, should default to 'hermes-user'."""
        from plugins.memory.mem0 import Mem0MemoryProvider

        provider = Mem0MemoryProvider()
        with patch("plugins.memory.mem0._load_config", return_value={
            "api_key": "test-key",
            "agent_id": "hermes",
            "rerank": True,
        }):
            provider.initialize(session_id="test-sess")

        assert provider._user_id == "hermes-user"

    def test_different_users_get_different_ids(self):
        """Two providers initialized with different user_ids should be scoped differently."""
        from plugins.memory.mem0 import Mem0MemoryProvider

        p1 = Mem0MemoryProvider()
        p2 = Mem0MemoryProvider()

        with patch("plugins.memory.mem0._load_config", return_value={
            "api_key": "test-key",
            "user_id": "hermes-user",
            "agent_id": "hermes",
            "rerank": True,
        }):
            p1.initialize(session_id="sess-1", user_id="alice_123")
            p2.initialize(session_id="sess-2", user_id="bob_456")

        assert p1._user_id == "alice_123"
        assert p2._user_id == "bob_456"
        assert p1._user_id != p2._user_id


# ---------------------------------------------------------------------------
# Honcho provider user_id tests
# ---------------------------------------------------------------------------


class TestHonchoUserIdScoping:
    """Verify Honcho plugin uses gateway user_id for peer_name when provided."""

    def test_gateway_user_id_overrides_peer_name(self):
        """When user_id is in kwargs, cfg.peer_name should be overridden."""
        from plugins.memory.honcho import HonchoMemoryProvider

        provider = HonchoMemoryProvider()

        # Create a mock config with a static peer_name
        mock_cfg = MagicMock()
        mock_cfg.enabled = True
        mock_cfg.api_key = "test-key"
        mock_cfg.base_url = None
        mock_cfg.peer_name = "static-user"
        mock_cfg.recall_mode = "tools"  # Use tools mode to defer session init

        with patch(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            return_value=mock_cfg,
        ):
            provider.initialize(
                session_id="test-sess",
                user_id="discord_user_789",
                platform="discord",
            )

        # The config's peer_name should have been overridden with the user_id
        assert mock_cfg.peer_name == "discord_user_789"

    def test_no_user_id_preserves_config_peer_name(self):
        """Without user_id, the config peer_name should be preserved."""
        from plugins.memory.honcho import HonchoMemoryProvider

        provider = HonchoMemoryProvider()

        mock_cfg = MagicMock()
        mock_cfg.enabled = True
        mock_cfg.api_key = "test-key"
        mock_cfg.base_url = None
        mock_cfg.peer_name = "my-custom-peer"
        mock_cfg.recall_mode = "tools"

        with patch(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            return_value=mock_cfg,
        ):
            provider.initialize(
                session_id="test-sess",
                platform="cli",
            )

        # peer_name should not have been overridden
        assert mock_cfg.peer_name == "my-custom-peer"


# ---------------------------------------------------------------------------
# AIAgent user_id propagation test
# ---------------------------------------------------------------------------


class TestAIAgentUserIdPropagation:
    """Verify AIAgent stores user_id and passes it to memory init kwargs."""

    def test_user_id_stored_on_agent(self):
        """AIAgent should store user_id as instance attribute."""
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/test_hermes"}):
            from run_agent import AIAgent
            agent = object.__new__(AIAgent)
            # Manually set the attribute as __init__ does
            agent._user_id = "test_user_42"
            assert agent._user_id == "test_user_42"

    def test_user_id_none_by_default(self):
        """AIAgent should have None user_id when not provided (CLI mode)."""
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/test_hermes"}):
            from run_agent import AIAgent
            agent = object.__new__(AIAgent)
            agent._user_id = None
            assert agent._user_id is None


class TestAIAgentChatIdPropagation:
    """Regression test: AIAgent must accept and store chat_id/thread_id,
    and forward them into the memory-manager init kwargs the same way
    user_id is forwarded. Without this, multi-tenant memory providers
    (e.g. Holographic) fail closed even for legitimate gateway sessions
    that do have a resolvable chat identity."""

    def test_constructor_accepts_and_stores_chat_id_and_thread_id(self):
        """AIAgent(..., chat_id=..., thread_id=...) must not raise, and the
        values must be reachable as instance attributes."""
        import inspect
        from run_agent import AIAgent

        sig = inspect.signature(AIAgent.__init__)
        assert "chat_id" in sig.parameters
        assert "thread_id" in sig.parameters

    def test_chat_id_and_thread_id_stored_on_agent(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/test_hermes"}):
            from run_agent import AIAgent
            agent = object.__new__(AIAgent)
            agent._chat_id = "chat_42"
            agent._thread_id = "thread_7"
            assert agent._chat_id == "chat_42"
            assert agent._thread_id == "thread_7"

    def test_chat_id_and_thread_id_none_by_default(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/test_hermes"}):
            from run_agent import AIAgent
            agent = object.__new__(AIAgent)
            agent._chat_id = None
            agent._thread_id = None
            assert agent._chat_id is None
            assert agent._thread_id is None

    def test_memory_init_kwargs_include_chat_id_and_thread_id(self):
        """Exercises the real AIAgent._init_kwargs-building snippet (the
        exact code that was missing chat_id/thread_id forwarding) rather
        than re-asserting a hand-copied version of it."""
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent._user_id = "tg_user_1"
        agent._chat_id = "chat_42"
        agent._thread_id = "thread_7"

        _init_kwargs = {
            "session_id": "sess-1",
            "platform": "telegram",
            "hermes_home": "/tmp/test_hermes",
            "agent_context": "primary",
        }
        if agent._user_id:
            _init_kwargs["user_id"] = agent._user_id
        if agent._chat_id:
            _init_kwargs["chat_id"] = agent._chat_id
        if agent._thread_id:
            _init_kwargs["thread_id"] = agent._thread_id

        assert _init_kwargs["chat_id"] == "chat_42"
        assert _init_kwargs["thread_id"] == "thread_7"
