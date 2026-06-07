"""Tests for multi-credential runtime pooling and rotation."""

from __future__ import annotations

import json
import time

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2))


def test_fill_first_selection_skips_recently_exhausted_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                        "last_status": "exhausted",
                        "last_status_at": time.time(),
                        "last_error_code": 402,
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "***",
                        "last_status": "ok",
                        "last_status_at": None,
                        "last_error_code": None,
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    entry = pool.select()

    assert entry is not None
    assert entry.id == "cred-2"
    assert pool.current().id == "cred-2"


def test_select_clears_expired_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-1",
                        "label": "old",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                        "last_status": "exhausted",
                        "last_status_at": time.time() - 90000,
                        "last_error_code": 402,
                    }
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    entry = pool.select()

    assert entry is not None
    assert entry.last_status == "ok"


def test_round_robin_strategy_rotates_priorities(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "***",
                    },
                ]
            },
        },
    )
    config_path = tmp_path / "hermes" / "config.yaml"
    config_path.write_text("credential_pool_strategies:\n  openrouter: round_robin\n")

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    first = pool.select()
    assert first is not None
    assert first.id == "cred-1"

    reloaded = load_pool("openrouter")
    second = reloaded.select()
    assert second is not None
    assert second.id == "cred-2"


def test_random_strategy_uses_random_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "***",
                    },
                ]
            },
        },
    )
    config_path = tmp_path / "hermes" / "config.yaml"
    config_path.write_text("credential_pool_strategies:\n  openrouter: random\n")

    monkeypatch.setattr("agent.credential_pool.random.choice", lambda entries: entries[-1])

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    selected = pool.select()
    assert selected is not None
    assert selected.id == "cred-2"



def test_exhausted_entry_resets_after_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-or-primary",
                        "base_url": "https://openrouter.ai/api/v1",
                        "last_status": "exhausted",
                        "last_status_at": time.time() - 90000,
                        "last_error_code": 429,
                    }
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    entry = pool.select()

    assert entry is not None
    assert entry.id == "cred-1"
    assert entry.last_status == "ok"


def test_explicit_reset_timestamp_overrides_default_429_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "weekly-reset",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "tok-1",
                        "last_status": "exhausted",
                        "last_status_at": time.time() - 7200,
                        "last_error_code": 429,
                        "last_error_reason": "device_code_exhausted",
                        "last_error_reset_at": time.time() + 7 * 24 * 60 * 60,
                    }
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    assert pool.has_available() is False
    assert pool.select() is None


def test_mark_exhausted_and_rotate_persists_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-ant-api-primary",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-ant-api-secondary",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    assert pool.select().id == "cred-1"

    next_entry = pool.mark_exhausted_and_rotate(status_code=402)

    assert next_entry is not None
    assert next_entry.id == "cred-2"

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["anthropic"][0]
    assert persisted["last_status"] == "exhausted"
    assert persisted["last_error_code"] == 402


def test_try_refresh_current_updates_only_current_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",
                        "access_token": "access-old",
                        "refresh_token": "refresh-old",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "device_code",
                        "access_token": "access-other",
                        "refresh_token": "refresh-other",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    monkeypatch.setattr(
        "hermes_cli.auth.refresh_codex_oauth_pure",
        lambda access_token, refresh_token, timeout_seconds=20.0: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
        },
    )

    pool = load_pool("openai-codex")
    current = pool.select()
    assert current.id == "cred-1"

    refreshed = pool.try_refresh_current()

    assert refreshed is not None
    assert refreshed.access_token == "access-new"

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    primary, secondary = auth_payload["credential_pool"]["openai-codex"]
    assert primary["access_token"] == "access-new"
    assert primary["refresh_token"] == "refresh-new"
    assert secondary["access_token"] == "access-other"
    assert secondary["refresh_token"] == "refresh-other"


def test_token_invalidated_marks_credential_dead(tmp_path, monkeypatch):
    """OpenAI Codex token_invalidated must mark the credential DEAD, not exhausted.

    Regression for #32849: when an OAuth credential is revoked upstream, the
    1-hour exhausted TTL means it re-enters rotation every hour and fails
    again with the same 401 — surfacing as "Failed to generate context
    summary" on context compression.  Terminal OAuth failures should never
    auto-recover.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-dead",
                        "label": "revoked",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "revoked-at",
                        "refresh_token": "revoked-rt",
                    },
                    {
                        "id": "cred-ok",
                        "label": "healthy",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "healthy-at",
                        "refresh_token": "healthy-rt",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool, STATUS_DEAD

    pool = load_pool("openai-codex")
    assert pool.select().id == "cred-dead"

    # Simulate the exact OpenAI Codex 401 token_invalidated response shape.
    next_entry = pool.mark_exhausted_and_rotate(
        status_code=401,
        error_context={
            "reason": "token_invalidated",
            "message": "Your authentication token has been invalidated. Please try signing in again.",
        },
    )

    # Rotation still works — we hand off to the healthy credential.
    assert next_entry is not None
    assert next_entry.id == "cred-ok"

    # The revoked credential is now permanently marked DEAD.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["openai-codex"][0]
    assert persisted["last_status"] == STATUS_DEAD
    assert persisted["last_error_code"] == 401
    assert persisted["last_error_reason"] == "token_invalidated"


def test_dead_credential_never_re_enters_rotation_after_ttl(tmp_path, monkeypatch):
    """A DEAD credential must stay excluded regardless of how much time passes.

    The exhausted TTL clears entries after 5 min (401) / 1 hour (429).
    A DEAD credential has no recovery TTL — it stays dead until either
    (a) an explicit re-auth write-side sync rewrites the tokens, or
    (b) the manual-prune TTL elapses (covered by separate tests below).
    This test verifies the core invariant in the recent-entry window.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # DEAD entry from 2 hours ago — well past the exhausted TTLs (5min/1h)
    # but well within the 24h manual-prune window.
    two_hours_ago = time.time() - (2 * 3600)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-dead",
                        "label": "revoked",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "revoked-at",
                        "refresh_token": "revoked-rt",
                        "last_status": "dead",
                        "last_status_at": two_hours_ago,
                        "last_error_code": 401,
                        "last_error_reason": "token_invalidated",
                    },
                    {
                        "id": "cred-ok",
                        "label": "healthy",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "healthy-at",
                        "refresh_token": "healthy-rt",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool, STATUS_DEAD

    pool = load_pool("openai-codex")
    selected = pool.select()
    # Should skip the dead entry and pick the healthy one — even though
    # the dead entry has priority 0 (would normally be picked first) and
    # plenty of time has passed since it was marked dead.
    assert selected is not None
    assert selected.id == "cred-ok"

    # The DEAD entry is still marked dead on disk — not cleared by TTL.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    dead_entry = next(e for e in auth_payload["credential_pool"]["openai-codex"]
                       if e["id"] == "cred-dead")
    assert dead_entry["last_status"] == STATUS_DEAD


def test_429_rate_limit_still_uses_exhausted_not_dead(tmp_path, monkeypatch):
    """429 rate limits must NOT be treated as terminal.

    They should keep the existing 1-hour TTL cooldown semantics so the
    credential re-enters rotation once the rate window resets.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "at-2",
                        "refresh_token": "rt-2",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool, STATUS_EXHAUSTED

    pool = load_pool("openai-codex")
    assert pool.select().id == "cred-1"

    next_entry = pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"reason": "rate_limit_exceeded", "message": "Rate limit exceeded"},
    )
    assert next_entry is not None
    assert next_entry.id == "cred-2"

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["openai-codex"][0]
    # 429 stays exhausted (transient) — NOT dead.
    assert persisted["last_status"] == STATUS_EXHAUSTED
    assert persisted["last_error_code"] == 429


def test_generic_401_without_terminal_reason_still_uses_exhausted(tmp_path, monkeypatch):
    """A 401 with no specific code/reason should keep TTL semantics.

    Only specific terminal reasons (token_invalidated, token_revoked, etc.)
    transition to DEAD.  A generic 401 might be a transient server-side
    issue worth retrying after the 5-min TTL.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "at-2",
                        "refresh_token": "rt-2",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool, STATUS_EXHAUSTED

    pool = load_pool("openai-codex")
    pool.select()

    # 401 with no specific reason — stays exhausted, NOT dead.
    pool.mark_exhausted_and_rotate(
        status_code=401,
        error_context={"message": "Unauthorized"},
    )

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["openai-codex"][0]
    assert persisted["last_status"] == STATUS_EXHAUSTED
    assert persisted["last_error_code"] == 401


def test_dead_manual_entry_pruned_after_24h(tmp_path, monkeypatch):
    """A DEAD manual entry is removed from the pool after the prune TTL.

    Manual entries (``manual:*``) are independent credentials with no
    singleton to re-seed from, so we can clean them up after a quiet
    window without losing recoverability — the user can always re-add
    via ``hermes auth add``.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # DEAD entry from > 24h ago
    long_ago = time.time() - (25 * 3600)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-old-dead",
                        "label": "ancient-dead",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "stale",
                        "refresh_token": "stale",
                        "last_status": "dead",
                        "last_status_at": long_ago,
                        "last_error_code": 401,
                        "last_error_reason": "token_invalidated",
                    },
                    {
                        "id": "cred-ok",
                        "label": "healthy",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "healthy-at",
                        "refresh_token": "healthy-rt",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    # Trigger _available_entries via select; that runs the prune.
    selected = pool.select()
    assert selected is not None
    assert selected.id == "cred-ok"

    # On-disk pool should have the dead entry removed.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["openai-codex"]
    assert len(persisted) == 1
    assert persisted[0]["id"] == "cred-ok"


def test_dead_manual_entry_kept_within_24h(tmp_path, monkeypatch):
    """A DEAD manual entry stays in the pool until the prune TTL elapses.

    Recent DEAD entries are kept so the audit trail (last_error_reason,
    timestamps) remains visible while the user investigates.  They simply
    don't participate in rotation (covered by the DEAD-skip test above).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # DEAD entry from only an hour ago — well within the 24h window
    recent = time.time() - 3600
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-recent-dead",
                        "label": "recent-dead",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "stale",
                        "refresh_token": "stale",
                        "last_status": "dead",
                        "last_status_at": recent,
                        "last_error_code": 401,
                        "last_error_reason": "token_invalidated",
                    },
                    {
                        "id": "cred-ok",
                        "label": "healthy",
                        "auth_type": "oauth",
                        "priority": 1,
                        "source": "manual:device_code",
                        "access_token": "healthy-at",
                        "refresh_token": "healthy-rt",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool, STATUS_DEAD

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected is not None
    assert selected.id == "cred-ok"

    # On-disk pool should still have BOTH entries — recent dead is preserved.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["openai-codex"]
    assert len(persisted) == 2
    dead_entry = next(e for e in persisted if e["id"] == "cred-recent-dead")
    assert dead_entry["last_status"] == STATUS_DEAD


def test_dead_singleton_seeded_entry_not_pruned(tmp_path, monkeypatch):
    """A DEAD ``device_code`` entry must NOT be pruned even after 24h.

    Singleton-seeded entries get re-created by ``_seed_from_singletons`` on
    every ``load_pool()``, so pruning them is pointless — they reappear
    immediately with the same stale singleton tokens.  Keep them visible
    with the DEAD marker so the user knows what's broken.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    long_ago = time.time() - (48 * 3600)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {"access_token": "revoked-at", "refresh_token": "revoked-rt"},
                    "last_refresh": "2026-01-01T00:00:00Z",
                    "auth_mode": "chatgpt",
                },
            },
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-seeded-dead",
                        "label": "seeded-dead",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "device_code",   # singleton-seeded, NOT manual
                        "access_token": "revoked-at",
                        "refresh_token": "revoked-rt",
                        "last_status": "dead",
                        "last_status_at": long_ago,
                        "last_error_code": 401,
                        "last_error_reason": "token_invalidated",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool, STATUS_DEAD

    pool = load_pool("openai-codex")
    # No healthy entry available; select returns None (pool empty for rotation).
    assert pool.select() is None

    # On-disk: the singleton-seeded DEAD entry is preserved.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    persisted = auth_payload["credential_pool"]["openai-codex"]
    assert len(persisted) == 1
    assert persisted[0]["id"] == "cred-seeded-dead"
    assert persisted[0]["last_status"] == STATUS_DEAD

def test_load_pool_seeds_env_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-seeded")
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    entry = pool.select()

    assert entry is not None
    assert entry.source == "env:OPENROUTER_API_KEY"
    assert entry.access_token == "sk-or-seeded"


def test_load_pool_removes_stale_seeded_env_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "seeded-env",
                        "label": "OPENROUTER_API_KEY",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "env:OPENROUTER_API_KEY",
                        "access_token": "stale-token",
                        "base_url": "https://openrouter.ai/api/v1",
                    }
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")

    assert pool.entries() == []

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    assert auth_payload["credential_pool"]["openrouter"] == []


def test_load_pool_migrates_nous_provider_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "active_provider": "nous",
            "providers": {
                "nous": {
                    "portal_base_url": "https://portal.example.com",
                    "inference_base_url": "https://inference.example.com/v1",
                    "client_id": "hermes-cli",
                    "token_type": "Bearer",
                    "scope": "inference:mint_agent_key",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_at": "2026-03-24T12:00:00+00:00",
                    "agent_key": "agent-key",
                    "agent_key_expires_at": "2026-03-24T13:30:00+00:00",
                }
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("nous")
    entry = pool.select()

    assert entry is not None
    assert entry.source == "device_code"
    assert entry.portal_base_url == "https://portal.example.com"
    assert entry.agent_key == "agent-key"


def test_load_pool_removes_stale_file_backed_singleton_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "seeded-file",
                        "label": "claude-code",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "claude_code",
                        "access_token": "stale-access-token",
                        "refresh_token": "stale-refresh-token",
                        "expires_at_ms": int(time.time() * 1000) + 60_000,
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(
        "agent.anthropic_adapter.read_hermes_oauth_credentials",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: None,
    )

    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")

    assert pool.entries() == []

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    assert auth_payload["credential_pool"]["anthropic"] == []


def test_load_pool_migrates_nous_provider_state_preserves_tls(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "active_provider": "nous",
            "providers": {
                "nous": {
                    "portal_base_url": "https://portal.example.com",
                    "inference_base_url": "https://inference.example.com/v1",
                    "client_id": "hermes-cli",
                    "token_type": "Bearer",
                    "scope": "inference:mint_agent_key",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_at": "2026-03-24T12:00:00+00:00",
                    "agent_key": "agent-key",
                    "agent_key_expires_at": "2026-03-24T13:30:00+00:00",
                    "tls": {
                        "insecure": True,
                        "ca_bundle": "/tmp/nous-ca.pem",
                    },
                }
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("nous")
    entry = pool.select()

    assert entry is not None
    assert entry.tls == {
        "insecure": True,
        "ca_bundle": "/tmp/nous-ca.pem",
    }

    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    assert auth_payload["credential_pool"]["nous"][0]["tls"] == {
        "insecure": True,
        "ca_bundle": "/tmp/nous-ca.pem",
    }


def test_singleton_seed_does_not_clobber_manual_oauth_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "manual-1",
                        "label": "manual-pkce",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:hermes_pkce",
                        "access_token": "manual-token",
                        "refresh_token": "manual-refresh",
                        "expires_at_ms": 1711234567000,
                    }
                ]
            },
        },
    )

    monkeypatch.setattr(
        "agent.anthropic_adapter.read_hermes_oauth_credentials",
        lambda: {
            "accessToken": "seeded-token",
            "refreshToken": "seeded-refresh",
            "expiresAt": 1711234999000,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: None,
    )

    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    entries = pool.entries()

    assert len(entries) == 2
    assert {entry.source for entry in entries} == {"manual:hermes_pkce", "hermes_pkce"}


def test_load_pool_prefers_anthropic_env_token_over_file_backed_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_TOKEN", "env-override-token")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})

    monkeypatch.setattr(
        "agent.anthropic_adapter.read_hermes_oauth_credentials",
        lambda: {
            "accessToken": "file-backed-token",
            "refreshToken": "refresh-token",
            "expiresAt": int(time.time() * 1000) + 3_600_000,
        },
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: None,
    )

    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    entry = pool.select()

    assert entry is not None
    assert entry.source == "env:ANTHROPIC_TOKEN"
    assert entry.access_token == "env-override-token"


def test_least_used_strategy_selects_lowest_count(tmp_path, monkeypatch):
    """least_used strategy should select the credential with the lowest request_count."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy",
        lambda _provider: "least_used",
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_env",
        lambda provider, entries: (False, set()),
    )
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "key-a",
                        "label": "heavy",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-or-heavy",
                        "request_count": 100,
                    },
                    {
                        "id": "key-b",
                        "label": "light",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-or-light",
                        "request_count": 10,
                    },
                    {
                        "id": "key-c",
                        "label": "medium",
                        "auth_type": "api_key",
                        "priority": 2,
                        "source": "manual",
                        "access_token": "sk-or-medium",
                        "request_count": 50,
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    entry = pool.select()
    assert entry is not None
    assert entry.id == "key-b"
    assert entry.access_token == "sk-or-light"


def test_mark_used_increments_request_count(tmp_path, monkeypatch):
    """mark_used should increment the request_count of the current entry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy",
        lambda _provider: "fill_first",
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_env",
        lambda provider, entries: (False, set()),
    )
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "key-a",
                        "label": "test",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-or-test",
                        "request_count": 5,
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    entry = pool.select()
    assert entry is not None
    assert entry.request_count == 5
    pool.mark_used()
    updated = pool.current()
    assert updated is not None
    assert updated.request_count == 6


def test_thread_safety_concurrent_select(tmp_path, monkeypatch):
    """Concurrent select() calls should not corrupt pool state."""
    import threading as _threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        "agent.credential_pool.get_pool_strategy",
        lambda _provider: "round_robin",
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_singletons",
        lambda provider, entries: (False, set()),
    )
    monkeypatch.setattr(
        "agent.credential_pool._seed_from_env",
        lambda provider, entries: (False, set()),
    )
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": f"key-{i}",
                        "label": f"key-{i}",
                        "auth_type": "api_key",
                        "priority": i,
                        "source": "manual",
                        "access_token": f"sk-or-{i}",
                    }
                    for i in range(5)
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    results = []
    errors = []

    def worker():
        try:
            for _ in range(20):
                entry = pool.select()
                if entry:
                    results.append(entry.id)
                    pool.mark_used(entry.id)
        except Exception as exc:
            errors.append(exc)

    threads = [_threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    assert len(results) == 80  # 4 threads * 20 selects


def test_custom_endpoint_pool_keyed_by_name(tmp_path, monkeypatch):
    """Verify load_pool('custom:together.ai') works and returns entries from auth.json."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # Disable seeding so we only test stored entries
    monkeypatch.setattr(
        "agent.credential_pool._seed_custom_pool",
        lambda pool_key, entries: (False, set()),
    )
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "custom:together.ai": [
                    {
                        "id": "cred-1",
                        "label": "together-key",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-together-xxx",
                        "base_url": "https://api.together.ai/v1",
                    },
                    {
                        "id": "cred-2",
                        "label": "together-key-2",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "sk-together-yyy",
                        "base_url": "https://api.together.ai/v1",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("custom:together.ai")
    assert pool.has_credentials()
    entries = pool.entries()
    assert len(entries) == 2
    assert entries[0].access_token == "sk-together-xxx"
    assert entries[1].access_token == "sk-together-yyy"

    # Select should return the first entry (fill_first default)
    entry = pool.select()
    assert entry is not None
    assert entry.id == "cred-1"


def test_custom_endpoint_pool_seeds_from_config(tmp_path, monkeypatch):
    """Verify seeding from custom_providers api_key in config.yaml."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1})

    # Write config.yaml with a custom_providers entry
    config_path = tmp_path / "hermes" / "config.yaml"
    import yaml
    config_path.write_text(yaml.dump({
        "custom_providers": [
            {
                "name": "Together.ai",
                "base_url": "https://api.together.ai/v1",
                "api_key": "sk-config-seeded",
            }
        ]
    }))

    from agent.credential_pool import load_pool

    pool = load_pool("custom:together.ai")
    assert pool.has_credentials()
    entries = pool.entries()
    assert len(entries) == 1
    assert entries[0].access_token == "sk-config-seeded"
    assert entries[0].source == "config:Together.ai"


def test_custom_endpoint_pool_seeds_from_model_config(tmp_path, monkeypatch):
    """Verify seeding from model.api_key when model.provider=='custom' and base_url matches."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1})

    import yaml
    config_path = tmp_path / "hermes" / "config.yaml"
    config_path.write_text(yaml.dump({
        "custom_providers": [
            {
                "name": "Together.ai",
                "base_url": "https://api.together.ai/v1",
            }
        ],
        "model": {
            "provider": "custom",
            "base_url": "https://api.together.ai/v1",
            "api_key": "sk-model-key",
        },
    }))

    from agent.credential_pool import load_pool

    pool = load_pool("custom:together.ai")
    assert pool.has_credentials()
    entries = pool.entries()
    # Should have the model_config entry
    model_entries = [e for e in entries if e.source == "model_config"]
    assert len(model_entries) == 1
    assert model_entries[0].access_token == "sk-model-key"


def test_custom_pool_does_not_break_existing_providers(tmp_path, monkeypatch):
    """Existing registry providers work exactly as before with custom pool support."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _write_auth_store(tmp_path, {"version": 1, "providers": {}})

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    entry = pool.select()
    assert entry is not None
    assert entry.source == "env:OPENROUTER_API_KEY"
    assert entry.access_token == "sk-or-test"


def test_get_custom_provider_pool_key(tmp_path, monkeypatch):
    """get_custom_provider_pool_key maps base_url to custom:<name> pool key."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir(parents=True, exist_ok=True)
    import yaml
    config_path = tmp_path / "hermes" / "config.yaml"
    config_path.write_text(yaml.dump({
        "custom_providers": [
            {
                "name": "Together.ai",
                "base_url": "https://api.together.ai/v1",
                "api_key": "sk-xxx",
            },
            {
                "name": "My Local Server",
                "base_url": "http://localhost:8080/v1",
            },
        ]
    }))

    from agent.credential_pool import get_custom_provider_pool_key

    assert get_custom_provider_pool_key("https://api.together.ai/v1") == "custom:together.ai"
    assert get_custom_provider_pool_key("https://api.together.ai/v1/") == "custom:together.ai"
    assert get_custom_provider_pool_key("http://localhost:8080/v1") == "custom:my-local-server"
    assert get_custom_provider_pool_key("https://unknown.example.com/v1") is None
    assert get_custom_provider_pool_key("") is None


def test_list_custom_pool_providers(tmp_path, monkeypatch):
    """list_custom_pool_providers returns custom: pool keys from auth.json."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "anthropic": [
                    {
                        "id": "a1",
                        "label": "test",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    }
                ],
                "custom:together.ai": [
                    {
                        "id": "c1",
                        "label": "together",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    }
                ],
                "custom:fireworks": [
                    {
                        "id": "c2",
                        "label": "fireworks",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    }
                ],
                "custom:empty": [],
            },
        },
    )

    from agent.credential_pool import list_custom_pool_providers

    result = list_custom_pool_providers()
    assert result == ["custom:fireworks", "custom:together.ai"]
    # "custom:empty" not included because it's empty



def test_acquire_lease_prefers_unleased_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    },
                    {
                        "id": "cred-2",
                        "label": "secondary",
                        "auth_type": "api_key",
                        "priority": 1,
                        "source": "manual",
                        "access_token": "***",
                    },
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    first = pool.acquire_lease()
    second = pool.acquire_lease()

    assert first == "cred-1"
    assert second == "cred-2"
    assert pool.active_lease_count("cred-1") == 1
    assert pool.active_lease_count("cred-2") == 1



def test_release_lease_decrements_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "cred-1",
                        "label": "primary",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "***",
                    }
                ]
            },
        },
    )

    from agent.credential_pool import load_pool

    pool = load_pool("openrouter")
    leased = pool.acquire_lease()
    assert leased == "cred-1"
    assert pool.active_lease_count("cred-1") == 1

    pool.release_lease("cred-1")
    assert pool.active_lease_count("cred-1") == 0


class TestLeastUsedStrategy:
    """Regression: least_used strategy must increment request_count on select."""

    def test_request_count_increments(self):
        """Each select() call should increment the chosen entry's request_count."""
        from unittest.mock import patch as _patch
        from agent.credential_pool import CredentialPool, PooledCredential, STRATEGY_LEAST_USED

        entries = [
            PooledCredential(provider="test", id="a", label="a", auth_type="api_key",
                             source="a", access_token="tok-a", priority=0, request_count=0),
            PooledCredential(provider="test", id="b", label="b", auth_type="api_key",
                             source="b", access_token="tok-b", priority=1, request_count=0),
        ]
        with _patch("agent.credential_pool.get_pool_strategy", return_value=STRATEGY_LEAST_USED):
            pool = CredentialPool("test", entries)

        # First select should pick entry with lowest count (both 0 → first)
        e1 = pool.select()
        assert e1 is not None
        count_after_first = e1.request_count
        assert count_after_first == 1, f"Expected 1 after first select, got {count_after_first}"

        # Second select should pick the OTHER entry (now has lower count)
        e2 = pool.select()
        assert e2 is not None
        assert e2.id != e1.id or e2.request_count == 2, (
            "least_used should alternate or increment"
        )

# ── OpenAI Codex OAuth cross-process sync tests ────────────────────────────

def _codex_auth_store(access: str, refresh: str) -> dict:
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": "id-" + access,
                },
                "last_refresh": "2026-04-28T00:00:00Z",
            }
        },
    }


def test_sync_codex_entry_from_auth_store_adopts_newer_tokens(tmp_path, monkeypatch):
    """When auth.json has newer Codex tokens, the pool entry should adopt them."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _codex_auth_store("access-OLD", "refresh-OLD"))

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = pool.select()
    assert entry is not None
    assert entry.access_token == "access-OLD"
    assert entry.refresh_token == "refresh-OLD"

    # Simulate `hermes auth openai-codex` replacing the token pair on disk.
    _write_auth_store(tmp_path, _codex_auth_store("access-NEW", "refresh-NEW"))

    synced = pool._sync_codex_entry_from_auth_store(entry)
    assert synced is not entry
    assert synced.access_token == "access-NEW"
    assert synced.refresh_token == "refresh-NEW"
    assert synced.last_status is None
    assert synced.last_error_code is None
    assert synced.last_error_reset_at is None


def test_sync_codex_entry_noop_when_tokens_match(tmp_path, monkeypatch):
    """When auth.json has the same tokens, sync should be a no-op."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, _codex_auth_store("access-same", "refresh-same"))

    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = pool.select()
    assert entry is not None

    synced = pool._sync_codex_entry_from_auth_store(entry)
    assert synced is entry


def test_codex_exhausted_entry_recovers_via_auth_store_sync(tmp_path, monkeypatch):
    """An exhausted Codex entry should recover when auth.json has newer tokens.

    Reproduces the Discord report (p1aceho1der, Apr 2026): after a Codex
    rate-limit reset the user ran `hermes model` to reauth, but the pool
    entry stayed marked EXHAUSTED with last_error_reset_at many hours in
    the future — so `_available_entries` kept returning empty and every
    request failed with "no available entries (all exhausted or empty)".
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from agent.credential_pool import load_pool, STATUS_EXHAUSTED
    from dataclasses import replace as dc_replace

    _write_auth_store(tmp_path, _codex_auth_store("access-OLD", "refresh-OLD"))

    pool = load_pool("openai-codex")
    entry = pool.select()
    assert entry is not None

    # Mark entry as exhausted with last_error_reset_at one hour in the
    # future (Codex 429 weekly-window pattern).
    now = time.time()
    exhausted = dc_replace(
        entry,
        last_status=STATUS_EXHAUSTED,
        last_status_at=now,
        last_error_code=429,
        last_error_reset_at=now + 3600,
    )
    pool._replace_entry(entry, exhausted)
    pool._persist()

    # Sanity: before the reauth, _available_entries refuses to return
    # this entry because last_error_reset_at is in the future.
    # (clear_expired would only clear it AFTER exhausted_until elapsed.)
    available_before = pool._available_entries(clear_expired=True, refresh=False)
    assert available_before == []

    # Simulate `hermes model` / `hermes auth` refreshing the tokens.
    _write_auth_store(tmp_path, _codex_auth_store("access-FRESH", "refresh-FRESH"))

    available = pool._available_entries(clear_expired=True, refresh=False)
    assert len(available) == 1
    assert available[0].access_token == "access-FRESH"
    assert available[0].refresh_token == "refresh-FRESH"
    assert available[0].last_status is None
    assert available[0].last_error_reset_at is None


def test_codex_exhausted_entry_stays_stuck_without_auth_store_update(tmp_path, monkeypatch):
    """Regression guard: if auth.json tokens haven't changed, the exhausted
    entry must stay stuck behind its reset window — sync must not spuriously
    clear status just because the entry is STATUS_EXHAUSTED."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    from agent.credential_pool import load_pool, STATUS_EXHAUSTED
    from dataclasses import replace as dc_replace

    _write_auth_store(tmp_path, _codex_auth_store("access-same", "refresh-same"))

    pool = load_pool("openai-codex")
    entry = pool.select()
    assert entry is not None

    now = time.time()
    exhausted = dc_replace(
        entry,
        last_status=STATUS_EXHAUSTED,
        last_status_at=now,
        last_error_code=429,
        last_error_reset_at=now + 3600,
    )
    pool._replace_entry(entry, exhausted)
    pool._persist()

    # auth.json unchanged → sync returns same entry → exhausted_until check
    # still skips it.
    available = pool._available_entries(clear_expired=True, refresh=False)
    assert available == []


# ---------------------------------------------------------------------------
# Codex OAuth terminal error quarantine
# ---------------------------------------------------------------------------


def _codex_auth_store(access_token: str, refresh_token: str) -> dict:
    return {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
            }
        },
    }


def test_is_terminal_codex_oauth_refresh_error():
    from hermes_cli.auth import AuthError, _is_terminal_codex_oauth_refresh_error

    assert _is_terminal_codex_oauth_refresh_error(
        AuthError("Refresh failed", provider="openai-codex", code="codex_refresh_failed", relogin_required=True)
    )
    assert _is_terminal_codex_oauth_refresh_error(
        AuthError("No token", provider="openai-codex", code="codex_auth_missing_refresh_token", relogin_required=True)
    )
    assert _is_terminal_codex_oauth_refresh_error(
        AuthError("Revoked", provider="openai-codex", code="invalid_grant", relogin_required=True)
    )
    assert _is_terminal_codex_oauth_refresh_error(
        AuthError("Reused", provider="openai-codex", code="refresh_token_reused", relogin_required=True)
    )
    # transient 429/5xx: relogin_required=False -> not terminal
    assert not _is_terminal_codex_oauth_refresh_error(
        AuthError("Rate limit", provider="openai-codex", code="codex_refresh_failed", relogin_required=False)
    )
    # xAI error does not trigger Codex check
    assert not _is_terminal_codex_oauth_refresh_error(
        AuthError("Revoked", provider="xai-oauth", code="xai_refresh_failed", relogin_required=True)
    )
    # Generic exception
    assert not _is_terminal_codex_oauth_refresh_error(ValueError("oops"))


def test_codex_oauth_terminal_refresh_clears_auth_json_and_removes_pool_entries(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_OAUTH_ACCESS_TOKEN", raising=False)

    _write_auth_store(tmp_path, _codex_auth_store("old-access-token", "old-refresh-token"))

    from agent.credential_pool import PooledCredential, load_pool
    import hermes_cli.auth as auth_mod
    from hermes_cli.auth import AuthError

    pool = load_pool("openai-codex")
    selected = pool.select()
    assert selected is not None
    assert selected.source == "device_code"

    # Add a manual API-key entry that must survive the quarantine.
    pool.add_entry(PooledCredential.from_dict("openai-codex", {
        "id": "manual-key",
        "source": "manual",
        "auth_type": "api_key",
        "access_token": "manual-codex-key",
    }))

    refresh_calls = {"count": 0}

    def _terminal_refresh_failure(*_args, **_kwargs):
        refresh_calls["count"] += 1
        raise AuthError(
            "Refresh session has been revoked",
            provider="openai-codex",
            code="codex_refresh_failed",
            relogin_required=True,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _terminal_refresh_failure)

    assert pool.try_refresh_current() is None

    # Only the manual entry survives.
    assert [entry.id for entry in pool.entries()] == ["manual-key"]

    # Auth.json tokens must be cleared.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    codex_state = auth_payload["providers"]["openai-codex"]
    tokens = codex_state.get("tokens", {})
    assert not tokens.get("access_token")
    assert not tokens.get("refresh_token")
    assert codex_state["last_auth_error"]["code"] == "codex_refresh_failed"
    assert codex_state["last_auth_error"]["relogin_required"] is True

    # Persisted pool must also have only the manual entry.
    assert [entry["id"] for entry in auth_payload["credential_pool"]["openai-codex"]] == ["manual-key"]

    # A second try_refresh_current must not call refresh_codex_oauth_pure again.
    assert pool.try_refresh_current() is None
    assert refresh_calls["count"] == 1


def test_codex_oauth_nonterminal_refresh_does_not_quarantine(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_OAUTH_ACCESS_TOKEN", raising=False)

    _write_auth_store(tmp_path, _codex_auth_store("old-access-token", "old-refresh-token"))

    from agent.credential_pool import load_pool
    import hermes_cli.auth as auth_mod
    from hermes_cli.auth import AuthError

    pool = load_pool("openai-codex")
    assert pool.select() is not None

    def _transient_failure(*_args, **_kwargs):
        raise AuthError(
            "Rate limited",
            provider="openai-codex",
            code="codex_refresh_failed",
            relogin_required=False,
        )

    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", _transient_failure)

    pool.try_refresh_current()

    # Tokens must NOT be cleared from auth.json.
    auth_payload = json.loads((tmp_path / "hermes" / "auth.json").read_text())
    tokens = auth_payload["providers"]["openai-codex"].get("tokens", {})
    assert tokens.get("access_token") == "old-access-token"
    assert tokens.get("refresh_token") == "old-refresh-token"