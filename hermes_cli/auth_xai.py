"""Minimal xAI Grok OAuth backport for the monolithic Hermes auth.py generation.

This intentionally does NOT depend on newer auth_constants.py/auth_codex.py,
profile-root write-through, credential-pool suppression, or quarantine machinery.
It reuses the existing auth.py lock/store/provider primitives.
"""

from __future__ import annotations

import base64
import json
import os
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _xai_err(message: str, code: str, *, relogin: bool = False):
    from hermes_cli.auth import AuthError
    return AuthError(
        message,
        provider="xai-oauth",
        code=code,
        relogin_required=relogin,
    )


def _is_xai_origin_host(host: str) -> bool:
    return host == "x.ai" or host.endswith(".x.ai")


def _validate_xai_url(url: str, *, field: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise _xai_err(f"xAI {field} must use HTTPS: {url!r}", "xai_discovery_invalid")
    if not host or not _is_xai_origin_host(host):
        raise _xai_err(
            f"xAI {field} host {host!r} is not x.ai or a *.x.ai subdomain.",
            "xai_discovery_invalid",
        )
    return url


def _xai_validate_inference_base_url(value: str, *, fallback: str = DEFAULT_XAI_OAUTH_BASE_URL) -> str:
    candidate = _clean(value).rstrip("/")
    if not candidate:
        return fallback
    try:
        return _validate_xai_url(candidate, field="base_url")
    except Exception:
        return fallback


def _xai_oauth_discovery(timeout_seconds: float = 15.0) -> Dict[str, str]:
    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise _xai_err(f"xAI OIDC discovery failed: {exc}", "xai_discovery_failed") from exc

    if response.status_code != 200:
        raise _xai_err(
            f"xAI OIDC discovery returned HTTP {response.status_code}.",
            "xai_discovery_failed",
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise _xai_err("xAI OIDC discovery returned invalid JSON.", "xai_discovery_invalid_json") from exc

    if not isinstance(payload, dict):
        raise _xai_err("xAI OIDC discovery response was not an object.", "xai_discovery_incomplete")

    result = {
        "authorization_endpoint": _clean(payload.get("authorization_endpoint")),
        "token_endpoint": _clean(payload.get("token_endpoint")),
    }
    if not all(result.values()):
        raise _xai_err("xAI OIDC discovery is missing required endpoints.", "xai_discovery_incomplete")

    for field, url in result.items():
        _validate_xai_url(url, field=field)
    return result


def _read_xai_oauth_tokens(*, _lock: bool = True) -> Dict[str, Any]:
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _load_provider_state

    if _lock:
        with _auth_store_lock():
            store = _load_auth_store()
    else:
        store = _load_auth_store()

    state = _load_provider_state(store, "xai-oauth")
    if not isinstance(state, dict):
        raise _xai_err(
            "No xAI OAuth credentials stored. Authenticate with xAI Grok OAuth first.",
            "xai_auth_missing",
            relogin=True,
        )

    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        raise _xai_err("xAI OAuth state is missing tokens.", "xai_auth_invalid_shape", relogin=True)

    access = _clean(tokens.get("access_token"))
    refresh = _clean(tokens.get("refresh_token"))
    if not access:
        raise _xai_err("xAI OAuth state is missing access_token.", "xai_auth_missing_access_token", relogin=True)
    if not refresh:
        raise _xai_err("xAI OAuth state is missing refresh_token.", "xai_auth_missing_refresh_token", relogin=True)

    return {
        "tokens": dict(tokens),
        "last_refresh": state.get("last_refresh"),
        "discovery": dict(state.get("discovery") or {}),
        "auth_mode": state.get("auth_mode") or "oauth_device_code",
    }


def _save_xai_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
    last_refresh: Optional[str] = None,
    set_active: bool = True,
) -> None:
    from hermes_cli.auth import (
        _auth_store_lock,
        _load_auth_store,
        _load_provider_state,
        _save_auth_store,
        _save_provider_state,
    )

    state = {
        "tokens": dict(tokens),
        "last_refresh": last_refresh or _utc_now_z(),
        "auth_mode": "oauth_device_code",
    }
    if discovery:
        state["discovery"] = dict(discovery)

    with _auth_store_lock():
        store = _load_auth_store()
        previous = _load_provider_state(store, "xai-oauth") or {}
        previous.update(state)
        _save_provider_state(store, "xai-oauth", previous)
        if not set_active:
            # _save_provider_state in this older auth.py always promotes the provider.
            # Restore the prior active provider for refreshes/status-side operations.
            old_active = store.get("active_provider")
            # old_active was overwritten, so recover it from the pre-save store snapshot is
            # impossible here; callers that need non-activation use direct store mutation below.
        _save_auth_store(store)


def _save_xai_oauth_tokens_preserve_active(
    tokens: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
    last_refresh: Optional[str] = None,
) -> None:
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _load_provider_state, _save_auth_store

    with _auth_store_lock():
        store = _load_auth_store()
        state = _load_provider_state(store, "xai-oauth") or {}
        state.update({
            "tokens": dict(tokens),
            "last_refresh": last_refresh or _utc_now_z(),
            "auth_mode": "oauth_device_code",
        })
        if discovery:
            state["discovery"] = dict(discovery)
        providers = store.setdefault("providers", {})
        providers["xai-oauth"] = state
        _save_auth_store(store)


def _xai_jwt_exp(access_token: Any) -> Optional[float]:
    if not isinstance(access_token, str) or access_token.count(".") < 1:
        return None
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        exp = decoded.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def _xai_access_token_is_expiring(access_token: str, skew_seconds: int = XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
    exp = _xai_jwt_exp(access_token)
    return exp is not None and exp <= time.time() + max(0, int(skew_seconds))


def _xai_oauth_request_device_code(client: httpx.Client, *, scope: str = XAI_OAUTH_SCOPE) -> Dict[str, Any]:
    response = client.post(
        XAI_OAUTH_DEVICE_CODE_URL,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        data={"client_id": XAI_OAUTH_CLIENT_ID, "scope": scope},
    )
    if response.status_code != 200:
        raise _xai_err(
            f"xAI device-code request failed (HTTP {response.status_code}).",
            "device_code_request_failed",
        )
    payload = response.json()
    required = ("device_code", "user_code", "verification_uri", "expires_in", "interval")
    missing = [key for key in required if key not in payload]
    if missing:
        raise _xai_err(
            f"xAI device-code response missing fields: {', '.join(missing)}",
            "device_code_invalid",
        )
    return payload


def _xai_oauth_poll_device_token(
    client: httpx.Client,
    *,
    token_endpoint: str,
    device_code: str,
    expires_in: int,
    poll_interval: int,
) -> Dict[str, Any]:
    _validate_xai_url(token_endpoint, field="token_endpoint")
    deadline = time.monotonic() + max(1, int(expires_in))
    interval = max(1, int(poll_interval))

    while time.monotonic() < deadline:
        response = client.post(
            token_endpoint,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": DEVICE_CODE_GRANT_TYPE,
                "client_id": XAI_OAUTH_CLIENT_ID,
                "device_code": device_code,
            },
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}

        if response.status_code == 200:
            if not _clean(payload.get("access_token")) or not _clean(payload.get("refresh_token")):
                raise _xai_err("xAI token response omitted required tokens.", "xai_device_token_invalid")
            return payload

        error = _clean(payload.get("error"))
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 1
            time.sleep(interval)
            continue

        detail = _clean(payload.get("error_description")) or error or response.text
        raise _xai_err(f"xAI device-code token polling failed: {detail}", "xai_device_token_failed")

    raise _xai_err("Timed out waiting for xAI device authorization.", "device_code_timeout")


def _tokens_from_payload(payload: Dict[str, Any], fallback_refresh: str = "") -> Dict[str, Any]:
    return {
        "access_token": _clean(payload.get("access_token")),
        "refresh_token": _clean(payload.get("refresh_token")) or fallback_refresh,
        "id_token": _clean(payload.get("id_token")),
        "expires_in": payload.get("expires_in"),
        "token_type": _clean(payload.get("token_type")) or "Bearer",
    }


def refresh_xai_oauth_pure(
    access_token: str,
    refresh_token: str,
    *,
    token_endpoint: str = "",
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    del access_token
    if not _clean(refresh_token):
        raise _xai_err("xAI OAuth is missing refresh_token.", "xai_auth_missing_refresh_token", relogin=True)

    endpoint = _clean(token_endpoint) or _xai_oauth_discovery(timeout_seconds)["token_endpoint"]
    _validate_xai_url(endpoint, field="token_endpoint")

    with httpx.Client(timeout=httpx.Timeout(max(5.0, timeout_seconds))) as client:
        response = client.post(
            endpoint,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )

    if response.status_code != 200:
        relogin = response.status_code in {400, 401}
        code = "xai_oauth_tier_denied" if response.status_code == 403 else "xai_refresh_failed"
        raise _xai_err(
            f"xAI token refresh failed (HTTP {response.status_code}).",
            code,
            relogin=relogin,
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise _xai_err("xAI token refresh returned invalid JSON.", "xai_refresh_invalid_json") from exc

    access = _clean(payload.get("access_token"))
    if not access:
        raise _xai_err("xAI token refresh omitted access_token.", "xai_refresh_missing_access_token")

    return {
        **_tokens_from_payload(payload, fallback_refresh=refresh_token),
        "last_refresh": _utc_now_z(),
    }


def resolve_xai_oauth_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> Dict[str, Any]:
    from hermes_cli.auth import AUTH_LOCK_TIMEOUT_SECONDS, _auth_store_lock

    data = _read_xai_oauth_tokens()
    tokens = dict(data["tokens"])
    access = _clean(tokens.get("access_token"))
    should_refresh = force_refresh or (
        refresh_if_expiring and _xai_access_token_is_expiring(access, refresh_skew_seconds)
    )

    if should_refresh:
        timeout_seconds = float(os.getenv("HERMES_XAI_REFRESH_TIMEOUT_SECONDS", "20"))
        with _auth_store_lock(timeout_seconds=max(float(AUTH_LOCK_TIMEOUT_SECONDS), timeout_seconds + 5.0)):
            data = _read_xai_oauth_tokens(_lock=False)
            tokens = dict(data["tokens"])
            access = _clean(tokens.get("access_token"))
            should_refresh = force_refresh or (
                refresh_if_expiring and _xai_access_token_is_expiring(access, refresh_skew_seconds)
            )
            if should_refresh:
                endpoint = _clean(data.get("discovery", {}).get("token_endpoint"))
                refreshed = refresh_xai_oauth_pure(
                    access,
                    _clean(tokens.get("refresh_token")),
                    token_endpoint=endpoint,
                    timeout_seconds=timeout_seconds,
                )
                tokens.update({
                    key: value for key, value in refreshed.items()
                    if key in {"access_token", "refresh_token", "id_token", "expires_in", "token_type"}
                })
                _save_xai_oauth_tokens_preserve_active(
                    tokens,
                    discovery=data.get("discovery"),
                    last_refresh=refreshed["last_refresh"],
                )
                data["last_refresh"] = refreshed["last_refresh"]

    override = os.getenv("HERMES_XAI_BASE_URL", "") or os.getenv("XAI_BASE_URL", "")
    return {
        "provider": "xai-oauth",
        "base_url": _xai_validate_inference_base_url(override),
        "api_key": _clean(tokens.get("access_token")),
        "source": "hermes-auth-store",
        "last_refresh": data.get("last_refresh"),
        "auth_mode": "oauth_device_code",
    }


def _xai_oauth_device_code_login(*, timeout_seconds: float = 20.0, open_browser: bool = True) -> Dict[str, Any]:
    discovery = _xai_oauth_discovery(timeout_seconds)
    timeout = httpx.Timeout(max(20.0, timeout_seconds))

    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        device = _xai_oauth_request_device_code(client)
        verify_url = _clean(device.get("verification_uri_complete")) or _clean(device["verification_uri"])

        print()
        print("Authorize xAI Grok OAuth:")
        print(f"  URL:  {verify_url}")
        print(f"  Code: {device['user_code']}")
        print()

        if open_browser:
            try:
                webbrowser.open(verify_url)
            except Exception:
                pass

        payload = _xai_oauth_poll_device_token(
            client,
            token_endpoint=discovery["token_endpoint"],
            device_code=_clean(device["device_code"]),
            expires_in=int(device["expires_in"]),
            poll_interval=int(device["interval"]),
        )

    return {
        "tokens": _tokens_from_payload(payload),
        "discovery": discovery,
        "base_url": DEFAULT_XAI_OAUTH_BASE_URL,
        "last_refresh": _utc_now_z(),
        "source": "oauth-device-code",
    }


def _login_xai_oauth(args, pconfig=None, *, force_new_login: bool = False) -> None:
    del pconfig
    if not force_new_login:
        try:
            existing = resolve_xai_oauth_runtime_credentials()
            if existing.get("api_key"):
                print("Existing xAI OAuth credentials found.")
                return
        except Exception:
            pass

    timeout_seconds = float(getattr(args, "timeout", None) or 20.0)
    open_browser = not bool(getattr(args, "no_browser", False))
    creds = _xai_oauth_device_code_login(
        timeout_seconds=timeout_seconds,
        open_browser=open_browser,
    )
    _save_xai_oauth_tokens(
        creds["tokens"],
        discovery=creds.get("discovery"),
        last_refresh=creds.get("last_refresh"),
        set_active=True,
    )

    from hermes_cli.auth import _update_config_for_provider
    config_path = _update_config_for_provider("xai-oauth", DEFAULT_XAI_OAUTH_BASE_URL)
    print("xAI Grok OAuth login successful.")
    print(f"  Config updated: {config_path} (model.provider=xai-oauth)")


def get_xai_oauth_auth_status() -> Dict[str, Any]:
    try:
        creds = resolve_xai_oauth_runtime_credentials(refresh_if_expiring=False)
        return {
            "logged_in": bool(creds.get("api_key")),
            "provider": "xai-oauth",
            "base_url": creds.get("base_url"),
            "last_refresh": creds.get("last_refresh"),
            "auth_mode": creds.get("auth_mode"),
            "source": creds.get("source"),
        }
    except Exception as exc:
        return {
            "logged_in": False,
            "provider": "xai-oauth",
            "error": str(exc),
        }
