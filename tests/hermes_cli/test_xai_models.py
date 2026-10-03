"""Tests for xAI Grok model discovery and retirement handling."""

from unittest.mock import patch

import pytest

from hermes_cli.xai_models import (
    DEFAULT_XAI_BASE_URL,
    DEFAULT_XAI_MODELS,
    RETIRED_XAI_MODELS,
    RetirementInfo,
    _dedupe_model_ids,
    _fetch_models_from_api,
    _filter_xai_models,
    _is_grok_model,
    format_xai_retirement_warning,
    get_recommended_xai_model,
    get_retirement_info,
    get_xai_model_ids,
    is_retired_xai_model,
    normalize_xai_model_id,
)


# =============================================================================
# Model normalization
# =============================================================================


def test_normalize_xai_model_id_bare_model():
    assert normalize_xai_model_id("grok-4.6") == "grok-4.6"


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("xai/grok-4.6", "grok-4.6"),
        ("x-ai/grok-4.6", "grok-4.6"),
        ("xai-oauth/grok-4.6", "grok-4.6"),
        ("XAI/GROK-4.6", "grok-4.6"),
        ("  xai/grok-4.6  ", "grok-4.6"),
    ],
)
def test_normalize_xai_model_id_strips_provider_prefix(
    model_id,
    expected,
):
    assert normalize_xai_model_id(model_id) == expected


def test_is_grok_model():
    assert _is_grok_model("grok-4.6") is True
    assert _is_grok_model("xai/grok-4.6") is True

    assert _is_grok_model("gpt-5.6-sol") is False
    assert _is_grok_model("claude-opus-4.6") is False
    assert _is_grok_model("") is False


# =============================================================================
# Deduplication
# =============================================================================


def test_dedupe_model_ids_preserves_order():
    models = _dedupe_model_ids(
        [
            "grok-4.6",
            "grok-4.3",
            "grok-4.6",
            "",
            "grok-4.3",
        ]
    )

    assert models == [
        "grok-4.6",
        "grok-4.3",
    ]


# =============================================================================
# Retirement handling
# =============================================================================


@pytest.mark.parametrize(
    "model_id",
    [
        "grok-3",
        "grok-4-0709",
        "grok-4-fast-reasoning",
        "grok-4-fast-non-reasoning",
        "grok-4-1-fast-reasoning",
        "grok-4-1-fast-non-reasoning",
        "grok-code-fast-1",
    ],
)
def test_known_retired_models_are_detected(model_id):
    assert is_retired_xai_model(model_id) is True


def test_retirement_detection_handles_provider_prefix():
    assert is_retired_xai_model(
        "xai-oauth/grok-3"
    ) is True


def test_current_model_is_not_retired():
    assert is_retired_xai_model("grok-4.6") is False


def test_get_retirement_info():
    info = get_retirement_info(
        "grok-4-fast-non-reasoning"
    )

    assert isinstance(info, RetirementInfo)

    assert info.model == "grok-4-fast-non-reasoning"
    assert info.replacement == "grok-4.3"
    assert info.reasoning_effort == "none"


def test_get_retirement_info_returns_none_for_current_model():
    assert get_retirement_info("grok-4.6") is None


def test_get_recommended_xai_model_for_retired_model():
    assert (
        get_recommended_xai_model("grok-3")
        == "grok-4.3"
    )


def test_get_recommended_xai_model_keeps_current_model():
    assert (
        get_recommended_xai_model("grok-4.6")
        == "grok-4.6"
    )


def test_all_retirement_entries_have_replacements():
    for model_id, entry in RETIRED_XAI_MODELS.items():
        assert model_id
        assert entry["replacement"]
        assert entry["replacement"].startswith("grok-")


# =============================================================================
# Retirement warning
# =============================================================================


def test_format_retirement_warning():
    warning = format_xai_retirement_warning(
        "grok-3"
    )

    assert warning is not None
    assert "grok-3" in warning
    assert "grok-4.3" in warning
    assert "retired" in warning.lower()


def test_format_retirement_warning_includes_reasoning_effort():
    warning = format_xai_retirement_warning(
        "grok-4-fast-non-reasoning"
    )

    assert warning is not None
    assert "reasoning_effort" in warning
    assert "none" in warning


def test_format_retirement_warning_returns_none_for_current_model():
    assert (
        format_xai_retirement_warning("grok-4.6")
        is None
    )


# =============================================================================
# Model filtering
# =============================================================================


def test_filter_xai_models_removes_non_grok_models():
    models = _filter_xai_models(
        [
            "grok-4.6",
            "gpt-5.6-sol",
            "claude-opus-4.6",
        ]
    )

    assert models == ["grok-4.6"]


def test_filter_xai_models_removes_retired_models():
    models = _filter_xai_models(
        [
            "grok-4.6",
            "grok-3",
            "grok-4-0709",
        ]
    )

    assert models == ["grok-4.6"]


def test_filter_xai_models_deduplicates():
    models = _filter_xai_models(
        [
            "grok-4.6",
            "grok-4.6",
            "xai/grok-4.6",
        ]
    )

    assert models == ["grok-4.6"]


def test_filter_xai_models_normalizes_provider_prefixes():
    models = _filter_xai_models(
        [
            "xai/grok-4.6",
            "xai-oauth/grok-4.5",
        ]
    )

    assert models == [
        "grok-4.6",
        "grok-4.5",
    ]


# =============================================================================
# API discovery
# =============================================================================


def test_fetch_models_from_api(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "grok-4.6"},
                    {"id": "grok-4.5"},
                    {"id": "gpt-5.6-sol"},
                ]
            }

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout

        return FakeResponse()

    monkeypatch.setattr(
        "httpx.get",
        fake_get,
    )

    models = _fetch_models_from_api(
        "xai-access-token"
    )

    assert models == [
        "grok-4.6",
        "grok-4.5",
    ]

    assert captured["url"] == (
        f"{DEFAULT_XAI_BASE_URL}/models"
    )

    assert captured["headers"]["Authorization"] == (
        "Bearer xai-access-token"
    )


def test_fetch_models_filters_retired_models(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "grok-4.6"},
                    {"id": "grok-3"},
                    {"id": "grok-4-0709"},
                ]
            }

    monkeypatch.setattr(
        "httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    models = _fetch_models_from_api(
        "xai-access-token"
    )

    assert models == ["grok-4.6"]


def test_fetch_models_returns_empty_when_api_fails(
    monkeypatch,
):
    class FakeResponse:
        status_code = 401

        def json(self):
            return {}

    monkeypatch.setattr(
        "httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    assert (
        _fetch_models_from_api(
            "invalid-token"
        )
        == []
    )


def test_fetch_models_returns_empty_on_network_error(
    monkeypatch,
):
    def raise_error(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(
        "httpx.get",
        raise_error,
    )

    assert (
        _fetch_models_from_api(
            "xai-access-token"
        )
        == []
    )


def test_fetch_models_returns_empty_for_invalid_json(
    monkeypatch,
):
    class FakeResponse:
        status_code = 200

        def json(self):
            raise ValueError("invalid json")

    monkeypatch.setattr(
        "httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    assert (
        _fetch_models_from_api(
            "xai-access-token"
        )
        == []
    )


def test_fetch_models_returns_empty_without_token():
    assert _fetch_models_from_api("") == []


# =============================================================================
# Public get_xai_model_ids()
# =============================================================================


def test_get_xai_model_ids_prefers_live_api(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.xai_models._fetch_models_from_api",
        lambda access_token, **kwargs: [
            "grok-4.6",
            "grok-4.5",
        ],
    )

    models = get_xai_model_ids(
        access_token="xai-access-token"
    )

    assert models == [
        "grok-4.6",
        "grok-4.5",
    ]


def test_get_xai_model_ids_passes_base_url(
    monkeypatch,
):
    captured = {}

    def fake_fetch(
        access_token,
        *,
        base_url,
        timeout_seconds=10.0,
    ):
        captured["access_token"] = access_token
        captured["base_url"] = base_url

        return ["grok-4.6"]

    monkeypatch.setattr(
        "hermes_cli.xai_models._fetch_models_from_api",
        fake_fetch,
    )

    models = get_xai_model_ids(
        access_token="xai-access-token",
        base_url="https://api.x.ai/v1",
    )

    assert models == ["grok-4.6"]

    assert (
        captured["access_token"]
        == "xai-access-token"
    )

    assert (
        captured["base_url"]
        == "https://api.x.ai/v1"
    )


def test_get_xai_model_ids_falls_back_to_defaults(
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.xai_models._fetch_models_from_api",
        lambda *args, **kwargs: [],
    )

    models = get_xai_model_ids(
        access_token="xai-access-token"
    )

    assert models == DEFAULT_XAI_MODELS


def test_get_xai_model_ids_without_token_uses_defaults():
    models = get_xai_model_ids()

    assert models == DEFAULT_XAI_MODELS


def test_get_xai_model_ids_uses_environment_base_url(
    monkeypatch,
):
    captured = {}

    monkeypatch.setenv(
        "HERMES_XAI_BASE_URL",
        "https://api.x.ai/v1",
    )

    def fake_fetch(
        access_token,
        *,
        base_url,
        timeout_seconds=10.0,
    ):
        captured["base_url"] = base_url

        return ["grok-4.6"]

    monkeypatch.setattr(
        "hermes_cli.xai_models._fetch_models_from_api",
        fake_fetch,
    )

    get_xai_model_ids(
        access_token="xai-access-token"
    )

    assert (
        captured["base_url"]
        == "https://api.x.ai/v1"
    )


# =============================================================================
# main.py model-flow integration
# =============================================================================


def test_model_command_uses_runtime_access_token_for_xai_list(
    monkeypatch,
):
    """xAI model flow should use the OAuth runtime token for discovery."""

    from hermes_cli.main import _model_flow_xai_oauth

    captured = {}

    monkeypatch.setattr(
        "hermes_cli.auth.get_xai_oauth_auth_status",
        lambda: {
            "logged_in": True,
        },
    )

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_xai_oauth_runtime_credentials",
        lambda: {
            "provider": "xai-oauth",
            "api_key": "xai-access-token",
            "base_url": "https://api.x.ai/v1",
        },
    )

    def fake_get_xai_model_ids(
        access_token=None,
        *,
        base_url=None,
    ):
        captured["access_token"] = access_token
        captured["base_url"] = base_url

        return [
            "grok-4.6",
            "grok-4.5",
        ]

    def fake_prompt_model_selection(
        model_ids,
        current_model="",
    ):
        captured["model_ids"] = list(model_ids)
        captured["current_model"] = current_model

        return None

    monkeypatch.setattr(
        "hermes_cli.xai_models.get_xai_model_ids",
        fake_get_xai_model_ids,
    )

    monkeypatch.setattr(
        "hermes_cli.xai_models.format_xai_retirement_warning",
        lambda model_id: None,
    )

    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        fake_prompt_model_selection,
    )

    _model_flow_xai_oauth(
        {},
        current_model="xai/grok-4.6",
    )

    assert (
        captured["access_token"]
        == "xai-access-token"
    )

    assert (
        captured["base_url"]
        == "https://api.x.ai/v1"
    )

    assert captured["model_ids"] == [
        "grok-4.6",
        "grok-4.5",
    ]

    assert (
        captured["current_model"]
        == "xai/grok-4.6"
    )


def test_model_command_warns_about_retired_xai_model(
    monkeypatch,
    capsys,
):
    """Existing retired Grok configuration should produce a warning."""

    from hermes_cli.main import _model_flow_xai_oauth

    monkeypatch.setattr(
        "hermes_cli.auth.get_xai_oauth_auth_status",
        lambda: {
            "logged_in": True,
        },
    )

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_xai_oauth_runtime_credentials",
        lambda: {
            "provider": "xai-oauth",
            "api_key": "xai-access-token",
            "base_url": "https://api.x.ai/v1",
        },
    )

    monkeypatch.setattr(
        "hermes_cli.xai_models.get_xai_model_ids",
        lambda *args, **kwargs: [
            "grok-4.6",
        ],
    )

    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda *args, **kwargs: None,
    )

    _model_flow_xai_oauth(
        {},
        current_model="grok-3",
    )

    output = capsys.readouterr().out

    assert "retired" in output.lower()
    assert "grok-3" in output
    assert "grok-4.3" in output