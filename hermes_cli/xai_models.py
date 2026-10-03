"""xAI Grok model discovery, fallback catalog, and retirement handling.

Responsibilities:
- Discover Grok models from the xAI /v1/models endpoint.
- Filter non-Grok models.
- Filter models known to be retired.
- Provide a conservative fallback catalog when live discovery fails.
- Detect retired models already present in Hermes configuration.
- Recommend replacements for retired models.

Authentication and OAuth token refresh belong in auth_xai.py.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"


# Conservative fallback catalog.
#
# Live API discovery should be preferred whenever an OAuth access token is
# available. Keep this list limited to models that Hermes should expose when
# live discovery is unavailable.
DEFAULT_XAI_MODELS: List[str] = [
    "grok-4.6",
]


# =============================================================================
# Retired models
# =============================================================================

# Models retired by xAI and their recommended replacements.
#
# reasoning_effort=None:
#     Do not modify reasoning_effort.
#
# reasoning_effort="none":
#     The old model represented a non-reasoning variant. Preserve that
#     behavior when migrating to the replacement model.
RETIRED_XAI_MODELS: Dict[str, Dict[str, Optional[str]]] = {
    "grok-3": {
        "replacement": "grok-4.3",
        "reasoning_effort": None,
    },
    "grok-4-0709": {
        "replacement": "grok-4.3",
        "reasoning_effort": None,
    },
    "grok-4-fast-reasoning": {
        "replacement": "grok-4.3",
        "reasoning_effort": None,
    },
    "grok-4-fast-non-reasoning": {
        "replacement": "grok-4.3",
        "reasoning_effort": "none",
    },
    "grok-4-1-fast-reasoning": {
        "replacement": "grok-4.3",
        "reasoning_effort": None,
    },
    "grok-4-1-fast-non-reasoning": {
        "replacement": "grok-4.3",
        "reasoning_effort": "none",
    },
    "grok-code-fast-1": {
        "replacement": "grok-4.3",
        "reasoning_effort": None,
    },
}


# =============================================================================
# Retirement model
# =============================================================================

@dataclass(frozen=True)
class RetirementInfo:
    """Information about a retired xAI model."""

    model: str
    replacement: str
    reasoning_effort: Optional[str] = None


# =============================================================================
# General helpers
# =============================================================================

def _clean(value: Any) -> str:
    """Return a stripped string or an empty string."""
    return str(value or "").strip()


def _dedupe_model_ids(model_ids: List[str]) -> List[str]:
    """Remove empty and duplicate model IDs while preserving order."""

    ordered: List[str] = []
    seen: set[str] = set()

    for model_id in model_ids:
        model_id = _clean(model_id)

        if not model_id:
            continue

        if model_id in seen:
            continue

        ordered.append(model_id)
        seen.add(model_id)

    return ordered


# =============================================================================
# Model normalization
# =============================================================================

def normalize_xai_model_id(model_id: str) -> str:
    """Normalize provider-prefixed xAI model IDs.

    Examples:

        grok-4.6
            -> grok-4.6

        xai/grok-4.6
            -> grok-4.6

        x-ai/grok-4.6
            -> grok-4.6

        xai-oauth/grok-4.6
            -> grok-4.6
    """

    normalized = _clean(model_id).lower()

    for prefix in (
        "x-ai/",
        "xai/",
        "xai-oauth/",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break

    return normalized


def _is_grok_model(model_id: str) -> bool:
    """Return True when model_id looks like a Grok model."""

    normalized = normalize_xai_model_id(model_id)

    return normalized.startswith("grok-")


# =============================================================================
# Retirement handling
# =============================================================================

def get_retirement_info(
    model_id: str,
) -> Optional[RetirementInfo]:
    """Return retirement information when model_id is retired."""

    normalized = normalize_xai_model_id(model_id)

    entry = RETIRED_XAI_MODELS.get(normalized)

    if not entry:
        return None

    return RetirementInfo(
        model=normalized,
        replacement=str(entry["replacement"]),
        reasoning_effort=entry.get("reasoning_effort"),
    )


def is_retired_xai_model(model_id: str) -> bool:
    """Return True when model_id is a retired xAI model."""

    return get_retirement_info(model_id) is not None


def get_recommended_xai_model(model_id: str) -> str:
    """Return replacement for a retired model.

    Non-retired models are returned unchanged.

    Example:

        grok-3
            -> grok-4.3

        grok-4.6
            -> grok-4.6
    """

    retirement = get_retirement_info(model_id)

    if retirement:
        return retirement.replacement

    return normalize_xai_model_id(model_id)


# =============================================================================
# Model filtering
# =============================================================================

def _filter_xai_models(model_ids: List[str]) -> List[str]:
    """Filter a model list for models Hermes should expose.

    Removes:
    - empty IDs
    - non-Grok models
    - retired Grok models
    - duplicates
    """

    result: List[str] = []

    for model_id in model_ids:
        normalized = normalize_xai_model_id(model_id)

        if not normalized:
            continue

        if not _is_grok_model(normalized):
            logger.debug(
                "Skipping non-Grok xAI model: %s",
                model_id,
            )
            continue

        if is_retired_xai_model(normalized):
            retirement = get_retirement_info(normalized)

            logger.debug(
                "Skipping retired xAI model %s; replacement=%s",
                normalized,
                retirement.replacement if retirement else "unknown",
            )
            continue

        result.append(normalized)

    return _dedupe_model_ids(result)


# =============================================================================
# Live xAI model discovery
# =============================================================================

def _fetch_models_from_api(
    access_token: str,
    *,
    base_url: str = DEFAULT_XAI_BASE_URL,
    timeout_seconds: float = 10.0,
) -> List[str]:
    """Fetch models visible to the authenticated xAI account.

    Calls:

        GET <base_url>/models

    Authentication:

        Authorization: Bearer <access_token>

    Returns an empty list on network/API/parsing failure. The public
    get_xai_model_ids() function will then use DEFAULT_XAI_MODELS.
    """

    access_token = _clean(access_token)

    if not access_token:
        return []

    base_url = _clean(base_url).rstrip("/")

    if not base_url:
        base_url = DEFAULT_XAI_BASE_URL

    url = f"{base_url}/models"

    try:
        import httpx

        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
        )

    except Exception as exc:
        logger.debug(
            "Failed to fetch xAI models from %s: %s",
            url,
            exc,
        )
        return []

    if response.status_code != 200:
        logger.debug(
            "xAI model discovery returned HTTP %s",
            response.status_code,
        )
        return []

    try:
        payload = response.json()

    except Exception as exc:
        logger.debug(
            "xAI model discovery returned invalid JSON: %s",
            exc,
        )
        return []

    if not isinstance(payload, dict):
        logger.debug(
            "xAI model discovery response was not a JSON object."
        )
        return []

    entries = payload.get("data")

    if not isinstance(entries, list):
        logger.debug(
            "xAI model discovery response did not contain data[]."
        )
        return []

    discovered: List[str] = []

    for item in entries:

        if not isinstance(item, dict):
            continue

        model_id = item.get("id")

        if not isinstance(model_id, str):
            continue

        model_id = model_id.strip()

        if not model_id:
            continue

        discovered.append(model_id)

    return _filter_xai_models(discovered)


# =============================================================================
# Public model discovery API
# =============================================================================

def get_xai_model_ids(
    access_token: Optional[str] = None,
    *,
    base_url: Optional[str] = None,
) -> List[str]:
    """Return available xAI Grok model IDs.

    Resolution order:

        1. Live xAI /models API
        2. DEFAULT_XAI_MODELS

    Live discovery is authoritative when it succeeds.

    The OAuth access token should normally come from:

        resolve_xai_oauth_runtime_credentials()

    Example:

        creds = resolve_xai_oauth_runtime_credentials()

        models = get_xai_model_ids(
            access_token=creds["api_key"],
            base_url=creds["base_url"],
        )
    """

    resolved_base_url = (
        _clean(base_url)
        or _clean(os.getenv("HERMES_XAI_BASE_URL"))
        or _clean(os.getenv("XAI_BASE_URL"))
        or DEFAULT_XAI_BASE_URL
    )

    # -----------------------------------------------------------------
    # Live discovery
    # -----------------------------------------------------------------

    if access_token:

        api_models = _fetch_models_from_api(
            access_token,
            base_url=resolved_base_url,
        )

        if api_models:
            return api_models

    # -----------------------------------------------------------------
    # Fallback
    # -----------------------------------------------------------------

    fallback_models = _filter_xai_models(
        list(DEFAULT_XAI_MODELS)
    )

    return fallback_models


# =============================================================================
# Configuration validation helpers
# =============================================================================

def check_current_xai_model(
    model_id: str,
) -> Optional[RetirementInfo]:
    """Check whether an existing configured model is retired.

    Intended for `hermes model` and startup validation.

    Returns:
        RetirementInfo when retired.
        None otherwise.
    """

    if not _clean(model_id):
        return None

    return get_retirement_info(model_id)


def format_xai_retirement_warning(
    model_id: str,
) -> Optional[str]:
    """Return a human-readable warning for a retired model."""

    info = get_retirement_info(model_id)

    if not info:
        return None

    message = (
        f"xAI model '{info.model}' is retired. "
        f"Recommended replacement: '{info.replacement}'."
    )

    if info.reasoning_effort:
        message += (
            f" Recommended reasoning_effort: "
            f"'{info.reasoning_effort}'."
        )

    return message