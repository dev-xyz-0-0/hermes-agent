"""Validated provider-usage anchors for read-only context diagnostics."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

from agent.model_metadata import estimate_messages_tokens_rough

logger = logging.getLogger(__name__)

USAGE_ANCHOR_MODEL_CONFIG_KEY = "_usage_anchor"
ANCHOR_VERSION = 1
_FINGERPRINT_KEYS = (
    "role", "content", "api_content", "tool_call_id", "tool_calls", "tool_name",
    "reasoning", "reasoning_details", "codex_reasoning_items",
)


def message_fingerprint(message: Any) -> Optional[str]:
    """Hash persisted, provider-relevant fields without retaining their contents."""
    if not isinstance(message, dict):
        return None
    payload = {key: message.get(key) for key in _FINGERPRINT_KEYS if message.get(key) is not None}
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def prefix_fingerprint(messages: list[dict[str, Any]], count: int) -> Optional[str]:
    if count <= 0 or len(messages) < count:
        return None
    fingerprints = [message_fingerprint(message) for message in messages[:count]]
    if any(not value for value in fingerprints):
        return None
    raw = json.dumps(fingerprints, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def model_identity(agent: Any) -> str:
    override = getattr(agent, "_usage_anchor_model_identity_override", None)
    if isinstance(override, str) and override:
        return override
    parts = (
        getattr(agent, "provider", ""), getattr(agent, "model", ""),
        getattr(agent, "api_mode", ""), getattr(agent, "base_url", ""),
    )
    raw = json.dumps([str(part or "") for part in parts], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def capture_usage_anchor(
    agent: Any,
    prompt_tokens: Any,
    completion_tokens: Any,
    messages: list[dict[str, Any]],
    *,
    response_message: Optional[dict[str, Any]] = None,
    request_messages: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """Bind provider usage to the exact durable transcript request boundary."""
    try:
        prompt = int(prompt_tokens or 0)
        completion = max(0, int(completion_tokens or 0))
    except (TypeError, ValueError):
        return None
    if prompt <= 0 or not isinstance(messages, list) or not messages:
        return None
    count = len(messages)
    prefix = prefix_fingerprint(messages, count)
    if not prefix:
        return None
    response_fp = message_fingerprint(response_message) if response_message else None
    return {
        "version": ANCHOR_VERSION,
        "session_id": str(getattr(agent, "session_id", "") or ""),
        "model_identity": model_identity(agent),
        "model": str(getattr(agent, "model", "") or ""),
        "provenance": "provider_usage",
        "observed_at": time.time(),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "transcript_length": count,
        "prefix_fingerprint": prefix,
        "last_message_fingerprint": message_fingerprint(messages[-1]),
        "response_fingerprint": response_fp,
        "request_fingerprint": prefix_fingerprint(
            request_messages, len(request_messages)
        ) if request_messages else prefix,
        "request_message_count": len(request_messages) if request_messages else count,
    }


def validate_usage_anchor(
    anchor: Any, messages: Any, *, session_id: str, model_key: str
) -> Optional[dict[str, Any]]:
    """Return normalized metadata only while all identity and prefix checks pass."""
    if not isinstance(anchor, dict) or not isinstance(messages, list):
        return None
    try:
        version = int(anchor.get("version"))
        prompt = int(anchor.get("prompt_tokens"))
        completion = max(0, int(anchor.get("completion_tokens", 0)))
        count = int(anchor.get("transcript_length"))
        observed_at = float(anchor.get("observed_at"))
    except (TypeError, ValueError):
        return None
    if version != ANCHOR_VERSION or prompt <= 0 or count <= 0 or len(messages) < count:
        return None
    if anchor.get("session_id") != session_id or anchor.get("model_identity") != model_key:
        return None
    prefix = anchor.get("prefix_fingerprint")
    if not isinstance(prefix, str) or prefix_fingerprint(messages, count) != prefix:
        return None
    normalized = dict(anchor)
    normalized.update(prompt_tokens=prompt, completion_tokens=completion,
                      transcript_length=count, observed_at=observed_at)
    return normalized


def anchored_context_tokens(
    messages: list[dict[str, Any]], anchor: Any, *, session_id: str, model_key: str
) -> Optional[tuple[int, bool, float]]:
    """Return prompt+completion plus an estimated valid appended-message delta."""
    valid = validate_usage_anchor(anchor, messages, session_id=session_id, model_key=model_key)
    if valid is None:
        return None
    delta = messages[valid["transcript_length"]:]
    response_fp = valid.get("response_fingerprint")
    if delta and response_fp and message_fingerprint(delta[0]) == response_fp:
        delta = delta[1:]
    elif delta and delta[0].get("role") == "assistant" and not response_fp:
        # Compatibility for an anchor captured before response normalization.
        delta = delta[1:]
    total = valid["prompt_tokens"] + valid["completion_tokens"]
    estimated = bool(delta)
    if delta:
        total += estimate_messages_tokens_rough(delta)
    return total, estimated, valid["observed_at"]


def persist_usage_anchor(agent: Any, anchor: Optional[dict[str, Any]]) -> None:
    patcher = getattr(getattr(agent, "_session_db", None), "patch_session_model_config", None)
    session_id = getattr(agent, "session_id", None)
    if not session_id or not callable(patcher):
        return
    try:
        patcher(session_id, {USAGE_ANCHOR_MODEL_CONFIG_KEY: anchor})
    except Exception:
        logger.debug("usage anchor persistence failed", exc_info=True)


def install_usage_anchor(agent: Any, anchor: Optional[dict[str, Any]]) -> None:
    agent._usage_anchor = anchor
    if getattr(agent, "_turn_base_usage_anchor", None) is None and anchor is not None:
        agent._turn_base_usage_anchor = anchor
    persist_usage_anchor(agent, anchor)


def restore_usage_anchor(agent: Any, messages: list[dict[str, Any]]) -> None:
    getter = getattr(getattr(agent, "_session_db", None), "get_session_model_config_value", None)
    if getattr(agent, "_usage_anchor", None) is not None or not callable(getter):
        return
    try:
        anchor = getter(agent.session_id, USAGE_ANCHOR_MODEL_CONFIG_KEY, None)
    except Exception:
        return
    if validate_usage_anchor(anchor, messages, session_id=str(agent.session_id), model_key=model_identity(agent)):
        agent._usage_anchor = anchor


def load_persisted_usage_anchor(session_db: Any, session_id: str) -> Optional[dict[str, Any]]:
    getter = getattr(session_db, "get_session_model_config_value", None)
    if not callable(getter):
        return None
    try:
        value = getter(session_id, USAGE_ANCHOR_MODEL_CONFIG_KEY, None)
    except Exception:
        return None
    return value if isinstance(value, dict) else None
