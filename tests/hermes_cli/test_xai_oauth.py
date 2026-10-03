import base64
import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import auth
from hermes_cli import auth_xai


def _jwt(exp: int) -> str:
    def enc(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc({'alg':'none'})}.{enc({'exp':exp})}.sig"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "_auth_file_path", lambda: auth_file)
    return auth_file


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_registry_contains_separate_xai_oauth_provider():
    assert auth.PROVIDER_REGISTRY["xai"].auth_type == "api_key"
    assert auth.PROVIDER_REGISTRY["xai-oauth"].auth_type == "oauth_external"
    assert auth.resolve_provider("grok") == "xai"
    assert auth.resolve_provider("grok-oauth") == "xai-oauth"


def test_save_and_read_tokens(isolated_store):
    tokens = {
        "access_token": _jwt(int(time.time()) + 7200),
        "refresh_token": "refresh-1",
        "token_type": "Bearer",
    }
    auth_xai._save_xai_oauth_tokens(
        tokens,
        discovery={"token_endpoint": "https://auth.x.ai/oauth2/token"},
    )

    loaded = auth_xai._read_xai_oauth_tokens()
    assert loaded["tokens"]["refresh_token"] == "refresh-1"
    assert auth.get_active_provider() == "xai-oauth"


def test_jwt_expiry_detection():
    assert auth_xai._xai_access_token_is_expiring(_jwt(int(time.time()) - 10), 0)
    assert not auth_xai._xai_access_token_is_expiring(_jwt(int(time.time()) + 7200), 60)


def test_discovery_rejects_non_xai_endpoint(monkeypatch):
    monkeypatch.setattr(
        auth_xai.httpx,
        "get",
        lambda *a, **k: FakeResponse(
            payload={
                "authorization_endpoint": "https://evil.example/authorize",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
            }
        ),
    )
    with pytest.raises(auth.AuthError) as exc:
        auth_xai._xai_oauth_discovery()
    assert exc.value.code == "xai_discovery_invalid"


def test_device_code_request():
    class Client:
        def post(self, *a, **k):
            return FakeResponse(payload={
                "device_code": "device-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://auth.x.ai/activate",
                "verification_uri_complete": "https://auth.x.ai/activate?code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 1,
            })

    data = auth_xai._xai_oauth_request_device_code(Client())
    assert data["device_code"] == "device-1"
    assert data["user_code"] == "ABCD-EFGH"


def test_poll_device_token_pending_then_success(monkeypatch):
    responses = iter([
        FakeResponse(status_code=400, payload={"error": "authorization_pending"}),
        FakeResponse(payload={"access_token": "access-1", "refresh_token": "refresh-1"}),
    ])

    class Client:
        def post(self, *a, **k):
            return next(responses)

    monkeypatch.setattr(auth_xai.time, "sleep", lambda _: None)
    payload = auth_xai._xai_oauth_poll_device_token(
        Client(),
        token_endpoint="https://auth.x.ai/oauth2/token",
        device_code="device-1",
        expires_in=30,
        poll_interval=1,
    )
    assert payload["access_token"] == "access-1"


def test_refresh_rotates_refresh_token(monkeypatch):
    class Client:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, *a, **k):
            return FakeResponse(payload={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "token_type": "Bearer",
            })

    monkeypatch.setattr(auth_xai.httpx, "Client", Client)
    result = auth_xai.refresh_xai_oauth_pure(
        "access-1",
        "refresh-1",
        token_endpoint="https://auth.x.ai/oauth2/token",
    )
    assert result["access_token"] == "access-2"
    assert result["refresh_token"] == "refresh-2"


def test_runtime_credentials_use_fresh_token_without_network(isolated_store):
    access = _jwt(int(time.time()) + 7200)
    auth_xai._save_xai_oauth_tokens(
        {"access_token": access, "refresh_token": "refresh-1"},
        discovery={"token_endpoint": "https://auth.x.ai/oauth2/token"},
    )
    creds = auth_xai.resolve_xai_oauth_runtime_credentials(
        refresh_if_expiring=True,
        refresh_skew_seconds=60,
    )
    assert creds["provider"] == "xai-oauth"
    assert creds["api_key"] == access
    assert creds["base_url"] == "https://api.x.ai/v1"


def test_runtime_refreshes_expiring_token(isolated_store, monkeypatch):
    old = _jwt(int(time.time()) + 10)
    new = _jwt(int(time.time()) + 7200)
    auth_xai._save_xai_oauth_tokens(
        {"access_token": old, "refresh_token": "refresh-1"},
        discovery={"token_endpoint": "https://auth.x.ai/oauth2/token"},
    )

    monkeypatch.setattr(
        auth_xai,
        "refresh_xai_oauth_pure",
        lambda *a, **k: {
            "access_token": new,
            "refresh_token": "refresh-2",
            "token_type": "Bearer",
            "id_token": "",
            "expires_in": 21600,
            "last_refresh": "2026-10-03T00:00:00Z",
        },
    )

    creds = auth_xai.resolve_xai_oauth_runtime_credentials(refresh_skew_seconds=60)
    assert creds["api_key"] == new
    assert auth_xai._read_xai_oauth_tokens()["tokens"]["refresh_token"] == "refresh-2"


def test_login_persists_and_updates_config(isolated_store, monkeypatch):
    access = _jwt(int(time.time()) + 7200)
    monkeypatch.setattr(
        auth_xai,
        "_xai_oauth_device_code_login",
        lambda **kwargs: {
            "tokens": {"access_token": access, "refresh_token": "refresh-login"},
            "discovery": {"token_endpoint": "https://auth.x.ai/oauth2/token"},
            "base_url": "https://api.x.ai/v1",
            "last_refresh": "2026-10-03T00:00:00Z",
        },
    )
    monkeypatch.setattr(auth, "_update_config_for_provider", lambda *a, **k: "config.yaml")

    auth_xai._login_xai_oauth(
        SimpleNamespace(timeout=3, no_browser=True),
        force_new_login=True,
    )
    assert auth.get_active_provider() == "xai-oauth"
    assert auth_xai._read_xai_oauth_tokens()["tokens"]["refresh_token"] == "refresh-login"


def test_status_does_not_expose_access_token(isolated_store):
    access = _jwt(int(time.time()) + 7200)
    auth_xai._save_xai_oauth_tokens(
        {"access_token": access, "refresh_token": "refresh-1"},
    )
    status = auth.get_auth_status("xai-oauth")
    assert status["logged_in"] is True
    assert "api_key" not in status
    assert access not in json.dumps(status)
