"""Phase 7.2D — minimal, stdlib-only Chrome DevTools Protocol client.

Test-only tooling (never imported by production code): drives a real
headless Chrome instance over its DevTools websocket to exercise actual
DOM/JS behavior the static source-level checks in
`test_holographic_viewer_frontend_security.py` can't reach (e.g. "does a
stale namespace's content actually leave the rendered DOM"). No
playwright/selenium/puppeteer dependency — just `socket`/`base64`/`json`,
matching the project's "no new production dependency" constraint (this is
a test dependency, and a zero-package one at that).

Any caller MUST treat browser unavailability as a skip, not a failure —
see `find_chrome()` and `ChromeProcess`'s `available` check.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request

_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def find_chrome() -> "str | None":
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class ChromeProcess:
    """A headless Chrome instance with its DevTools endpoint reachable.

    Raises `RuntimeError` on any startup problem — callers should catch
    this and skip the test, never fail it: a sandboxed/unusual CI
    environment where headless Chrome can't start is an environment
    limitation, not a product defect.
    """

    def __init__(self, user_data_dir: str, timeout: float = 10.0):
        chrome_path = find_chrome()
        if not chrome_path:
            raise RuntimeError("no headless-Chrome-compatible binary found on PATH")
        self.port = _free_port()
        self.process = subprocess.Popen(
            [
                chrome_path,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--remote-debugging-port={self.port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={user_data_dir}",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1) as r:
                    json.loads(r.read())
                    return
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                last_error = exc
                time.sleep(0.2)
        self.close()
        raise RuntimeError(f"headless Chrome never became reachable on port {self.port}: {last_error}")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class _WebSocket:
    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP websocket handshake closed early")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {resp[:200]!r}")

    def send(self, data: bytes) -> None:
        length = len(data)
        mask = os.urandom(4)
        if length < 126:
            header = struct.pack("!BB", 0x81, length | 0x80)
        elif length < 65536:
            header = struct.pack("!BBH", 0x81, 126 | 0x80, length)
        else:
            header = struct.pack("!BBQ", 0x81, 127 | 0x80, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("CDP websocket closed")
            buf += chunk
        return buf

    def recv(self) -> bytes:
        b0, b1 = self._recv_exact(2)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            raise RuntimeError("CDP websocket closed by peer")
        if opcode == 0x9:
            return self.recv()
        return payload


class CDPTab:
    """One browser tab, driven over its own CDP websocket.

    Collects console messages / CSP violations / exceptions as they
    arrive so tests can assert on them after driving the page.
    """

    def __init__(self, chrome: ChromeProcess):
        self.host, self.port = "127.0.0.1", chrome.port
        req = urllib.request.Request(f"http://{self.host}:{self.port}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req) as r:
            info = json.loads(r.read())
        self.target_id = info["id"]
        ws_url = info["webSocketDebuggerUrl"]
        path = ws_url.split(f"{self.host}:{self.port}", 1)[1]
        self.ws = _WebSocket(self.host, self.port, path)
        self._id = 0
        self.console_messages: list[dict] = []
        self.csp_violations: list[str] = []
        self.exceptions: list[dict] = []
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Log.enable")

    def send(self, method: str, params: "dict | None" = None, timeout: float = 10.0):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode())
        return self._await_result(msg_id, timeout=timeout)

    def _await_result(self, msg_id: int, timeout: float):
        self.ws.sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self.ws.recv()
            except socket.timeout:
                raise RuntimeError(f"CDP timed out waiting for id={msg_id}")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._dispatch_event(msg)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result", {})
        raise RuntimeError(f"CDP timed out waiting for id={msg_id}")

    def _dispatch_event(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "Runtime.consoleAPICalled":
            params = msg["params"]
            text = " ".join(
                str(a.get("value", a.get("description", a.get("type"))))
                for a in params.get("args", [])
            )
            self.console_messages.append({"level": params.get("type"), "text": text})
        elif method == "Runtime.exceptionThrown":
            self.exceptions.append(msg["params"])
        elif method == "Log.entryAdded":
            entry = msg["params"]["entry"]
            text = entry.get("text", "")
            if "Content Security Policy" in text:
                self.csp_violations.append(text)
            self.console_messages.append({"level": entry.get("level"), "text": text})

    def navigate(self, url: str, wait_s: float = 1.5) -> None:
        self.send("Page.navigate", {"url": url})
        self.drain(wait_s)

    def drain(self, wait_s: float = 0.5) -> None:
        self.ws.sock.settimeout(wait_s)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            try:
                raw = self.ws.recv()
            except socket.timeout:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._dispatch_event(msg)

    def eval(self, expression: str, await_promise: bool = False, timeout: float = 10.0):
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            urllib.request.urlopen(f"http://{self.host}:{self.port}/json/close/{self.target_id}", timeout=2).read()
        except Exception:
            pass
        try:
            self.ws.sock.close()
        except Exception:
            pass
