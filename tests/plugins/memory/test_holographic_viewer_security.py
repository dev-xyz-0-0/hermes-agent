"""Phase 7.1A — HTTP security hardening tests for the Holographic memory
viewer (`plugins/memory/holographic/viewer.py`).

Covers Host header validation (A1), Origin validation (A2), security
response headers (A3), request limits (A4), method policy incl. HEAD/
OPTIONS/TRACE/CONNECT (A5), and secure error handling (A6). Namespace
isolation, SQLite reliability, and pagination/search behavior are covered
by `test_holographic_viewer.py` and are out of scope here.

Reuses the `_start_server`/`_RunningServer`/`seeded_db` fixtures from
`test_holographic_viewer.py` rather than duplicating them. A raw-socket
helper is added here because `http.client` always sends a correct `Host`
header automatically and provides no way to omit or corrupt it — exactly
the cases this phase needs to exercise server-side.
"""

from __future__ import annotations

import socket
import threading

import pytest

from plugins.memory.holographic import viewer as _viewer
from plugins.memory.holographic.diagnostics import _namespace_digest
from plugins.memory.holographic.store import MemoryStore

from .test_holographic_viewer import _RunningServer, _start_server, seeded_db  # noqa: F401 (fixture)


# ---------------------------------------------------------------------------
# Raw-socket client — full control over the request line and headers,
# including omitting Host entirely or sending a malformed one.
# ---------------------------------------------------------------------------

def _raw_request(
    host: str, port: int, method: str, target: str, *,
    headers: "dict[str, str] | None" = None,
    omit_host: bool = False,
    timeout: float = 5.0,
) -> tuple[int, dict, bytes]:
    """Send a hand-built HTTP/1.1 request and return (status, headers, body).

    `headers={"Host": "..."}` sets a custom Host value; `omit_host=True`
    sends no Host header at all (not a real browser, but exercises the
    server's own validation independent of what any particular client
    library would or wouldn't normally send).
    """
    headers = dict(headers or {})
    lines = [f"{method} {target} HTTP/1.1"]
    if not omit_host and "Host" not in headers:
        headers["Host"] = f"{host}:{port}"
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    lines.append("")
    lines.append("")
    request_bytes = "\r\n".join(lines).encode("utf-8")

    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(request_bytes)
        sock.settimeout(timeout)
        chunks = []
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError:
            pass
        data = b"".join(chunks)
    finally:
        sock.close()

    head, _, body = data.partition(b"\r\n\r\n")
    head_lines = head.decode("iso-8859-1").split("\r\n")
    status = int(head_lines[0].split(" ", 2)[1])
    resp_headers: dict[str, str] = {}
    for line in head_lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            resp_headers[k.strip().lower()] = v.strip()
    return status, resp_headers, body


@pytest.fixture
def running(seeded_db):
    db_path, ns_a, ns_b = seeded_db
    s = _start_server(db_path)
    s.ns_a = ns_a
    s.ns_b = ns_b
    yield s
    s.stop()


# ---------------------------------------------------------------------------
# A1 — Host header validation
# ---------------------------------------------------------------------------

class TestHostHeaderValidation:
    def test_valid_loopback_host_accepted_for_every_route(self, running):
        digest_a = _namespace_digest(running.ns_a)
        for path in ("/", f"/namespace?ns={digest_a}"):
            resp = running.get(path)
            assert resp.status == 200

    def test_localhost_hostname_with_correct_port_accepted(self, running):
        status, _, _ = _raw_request(
            "127.0.0.1", running.port, "GET", "/",
            headers={"Host": f"localhost:{running.port}"},
        )
        assert status == 200

    def test_unknown_host_rejected(self, running):
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Host": "evil.example.com"},
        )
        assert status == 400

    def test_correct_hostname_wrong_port_rejected(self, running):
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Host": f"127.0.0.1:{running.port + 1}"},
        )
        assert status == 400

    def test_missing_host_header_rejected(self, running):
        status, _, _ = _raw_request(running.host, running.port, "GET", "/", omit_host=True)
        assert status == 400

    @pytest.mark.parametrize("bad_host", [
        "127.0.0.1:not-a-port",
        "user@127.0.0.1:{port}",
        "",
        "::1",  # unbracketed IPv6 is ambiguous/malformed in a Host header
    ])
    def test_malformed_host_header_rejected(self, running, bad_host):
        value = bad_host.format(port=running.port) if "{port}" in bad_host else bad_host
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Host": value} if value else {}, omit_host=not value,
        )
        assert status == 400

    def test_ipv6_bracket_syntax_recognized_by_the_validator(self):
        """`ViewerServer` binds an AF_INET socket (pre-existing, out of
        scope for this phase — see `LOOPBACK_HOSTS`/`_validate_host`), so an
        actual `::1` bind isn't exercised end-to-end here. This unit-tests
        the Host-header parser directly: bracketed IPv6 loopback with a
        matching port is recognized as valid, matching the port is still
        required, and an unbracketed `::1` (ambiguous with a port
        separator) is rejected as malformed."""
        assert _viewer._is_valid_host_header("[::1]:8765", 8765) is True
        assert _viewer._is_valid_host_header("[::1]:8765", 9999) is False
        assert _viewer._is_valid_host_header("::1:8765", 8765) is False


# ---------------------------------------------------------------------------
# A2 — Origin header validation
# ---------------------------------------------------------------------------

class TestOriginHeaderValidation:
    def test_absent_origin_is_accepted(self, running):
        resp = running.get("/")
        assert resp.status == 200

    def test_same_origin_accepted(self, running):
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Origin": f"http://127.0.0.1:{running.port}"},
        )
        assert status == 200

    def test_external_origin_rejected(self, running):
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Origin": "https://evil.example.com"},
        )
        assert status == 403

    def test_null_origin_rejected(self, running):
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Origin": "null"},
        )
        assert status == 403

    def test_loopback_origin_wrong_port_rejected(self, running):
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Origin": f"http://127.0.0.1:{running.port + 1}"},
        )
        assert status == 403

    def test_https_scheme_origin_rejected(self, running):
        """The viewer only ever serves plain http — an https Origin can
        never be a legitimate same-origin request."""
        status, _, _ = _raw_request(
            running.host, running.port, "GET", "/",
            headers={"Origin": f"https://127.0.0.1:{running.port}"},
        )
        assert status == 403


# ---------------------------------------------------------------------------
# A3 — security headers present on every response
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    _REQUIRED = {
        "content-security-policy": "default-src 'none'",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "cache-control": "no-store",
    }

    def _assert_headers(self, headers: dict) -> None:
        for name, expected_substr in self._REQUIRED.items():
            assert name in headers, f"missing {name}"
            assert expected_substr in headers[name]

    def test_headers_present_on_200(self, running):
        status, headers, _ = _raw_request(running.host, running.port, "GET", "/")
        assert status == 200
        self._assert_headers(headers)

    def test_headers_present_on_400(self, running):
        status, headers, _ = _raw_request(
            running.host, running.port, "GET", "/", headers={"Host": "evil.com"},
        )
        assert status == 400
        self._assert_headers(headers)

    def test_headers_present_on_403(self, running):
        status, headers, _ = _raw_request(
            running.host, running.port, "GET", "/", headers={"Origin": "https://evil.example.com"},
        )
        assert status == 403
        self._assert_headers(headers)

    def test_headers_present_on_404(self, running):
        status, headers, _ = _raw_request(running.host, running.port, "GET", "/does-not-exist")
        assert status == 404
        self._assert_headers(headers)

    def test_headers_present_on_405(self, running):
        status, headers, _ = _raw_request(running.host, running.port, "POST", "/")
        assert status == 405
        self._assert_headers(headers)

    def test_headers_present_on_500(self, tmp_path):
        """A valid database at startup that becomes unreadable afterward
        (e.g. truncated/corrupted on disk while the viewer is already
        running) still forces the generic catch-all 500 path, with no
        detail leaked. A file that's already invalid AT startup is instead
        rejected earlier, by `create_server` itself — see Phase 7.1C's
        `UnsupportedSchemaError` and `test_holographic_viewer_sqlite.py`."""
        from plugins.memory.holographic.store import MemoryStore

        db_path = tmp_path / "memory_store.db"
        MemoryStore(db_path=db_path, hrr_dim=32).close()
        server = _viewer.create_server(db_path, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            db_path.write_bytes(b"not a real sqlite database any more")
            port = server.server_address[1]
            status, headers, body = _raw_request("127.0.0.1", port, "GET", "/")
            assert status == 500
            self._assert_headers(headers)
            assert str(db_path) not in body.decode("utf-8", errors="replace")
            assert b"Traceback" not in body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ---------------------------------------------------------------------------
# A4 — request limits
# ---------------------------------------------------------------------------

class TestRequestLimits:
    def test_oversized_request_target_rejected(self, running):
        huge_path = "/" + ("a" * (_viewer.MAX_REQUEST_TARGET_LENGTH + 100))
        status, _, _ = _raw_request(running.host, running.port, "GET", huge_path)
        assert status == 414

    def test_oversized_query_string_rejected(self, running):
        huge_query = "x=" + ("a" * (_viewer.MAX_QUERY_STRING_LENGTH + 100))
        status, _, _ = _raw_request(running.host, running.port, "GET", f"/?{huge_query}")
        assert status == 414

    def test_too_many_query_params_rejected(self, running):
        digest_a = _namespace_digest(running.ns_a)
        extra = "&".join(f"p{i}=1" for i in range(_viewer.MAX_QUERY_PARAM_COUNT + 5))
        resp = running.get(f"/namespace?ns={digest_a}&{extra}")
        assert resp.status == 400

    def test_oversized_search_term_rejected(self, running):
        digest_a = _namespace_digest(running.ns_a)
        q = "a" * (_viewer.MAX_SEARCH_LENGTH + 10)
        resp = running.get(f"/namespace?ns={digest_a}&q={q}")
        assert resp.status == 400

    def test_search_term_at_limit_is_accepted(self, running):
        digest_a = _namespace_digest(running.ns_a)
        q = "a" * _viewer.MAX_SEARCH_LENGTH
        resp = running.get(f"/namespace?ns={digest_a}&q={q}")
        assert resp.status == 200

    def test_concurrency_cap_returns_503(self, seeded_db):
        db_path, _, _ = seeded_db
        server = _viewer.create_server(db_path, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            # Hold every slot directly rather than orchestrating real slow
            # requests across threads.
            acquired = [server.request_semaphore.acquire(blocking=False) for _ in range(_viewer.MAX_CONCURRENT_REQUESTS)]
            assert all(acquired)
            try:
                status, _, _ = _raw_request("127.0.0.1", port, "GET", "/")
                assert status == 503
            finally:
                for _ in acquired:
                    server.request_semaphore.release()
            # Once released, a normal request succeeds again.
            status, _, _ = _raw_request("127.0.0.1", port, "GET", "/")
            assert status == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ---------------------------------------------------------------------------
# A5 — method policy, including HEAD/OPTIONS/TRACE/CONNECT
# ---------------------------------------------------------------------------

class TestMethodPolicy:
    @pytest.mark.parametrize("method", ["OPTIONS", "TRACE", "CONNECT", "POST", "PUT", "DELETE", "PATCH"])
    @pytest.mark.parametrize("path", ["/", "/namespace", "/fact", "/entity", "/does-not-exist"])
    def test_non_get_head_methods_rejected_with_allow_header(self, running, method, path):
        status, headers, _ = _raw_request(running.host, running.port, method, path)
        assert status == 405
        assert headers.get("allow") == "GET, HEAD"

    def test_head_on_index_returns_200_with_no_body(self, running):
        status, headers, body = _raw_request(running.host, running.port, "HEAD", "/")
        assert status == 200
        assert body == b""
        assert int(headers["content-length"]) > 0

    def test_head_on_namespace_route_returns_200_with_no_body(self, running):
        digest_a = _namespace_digest(running.ns_a)
        status, headers, body = _raw_request(running.host, running.port, "HEAD", f"/namespace?ns={digest_a}")
        assert status == 200
        assert body == b""
        assert int(headers["content-length"]) > 0

    def test_head_with_bad_host_still_rejected_with_no_body(self, running):
        status, headers, body = _raw_request(
            running.host, running.port, "HEAD", "/", headers={"Host": "evil.com"},
        )
        assert status == 400
        assert body == b""


# ---------------------------------------------------------------------------
# A6 — secure error handling: no sensitive detail ever leaks
# ---------------------------------------------------------------------------

class TestSecureErrorHandling:
    def test_no_response_leaks_db_path_or_traceback(self, running, tmp_path):
        digest_a = _namespace_digest(running.ns_a)
        probes = [
            ("GET", "/", {}),
            ("GET", "/", {"Host": "evil.com"}),
            ("GET", "/", {"Origin": "https://evil.example.com"}),
            ("GET", "/does-not-exist", {}),
            ("POST", "/", {}),
            ("GET", f"/namespace?ns={digest_a}&q=" + "x" * (_viewer.MAX_SEARCH_LENGTH + 1), {}),
        ]
        for method, path, headers in probes:
            status, _, body = _raw_request(running.host, running.port, method, path, headers=headers)
            text = body.decode("utf-8", errors="replace")
            assert "memory_store.db" not in text
            assert "Traceback" not in text
            assert "/home/" not in text and "/Users/" not in text
