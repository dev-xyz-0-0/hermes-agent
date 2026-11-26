"""Thread-safe state for gateway clarify prompts."""

from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ClarifyRequest:
    request_id: int
    session_key: str
    question: str
    choices: Optional[List[str]]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None
    awaiting_text: bool = False
    cancelled: bool = False


_lock = threading.RLock()
logger = logging.getLogger(__name__)
_next_id = 0
_requests: Dict[str, List[ClarifyRequest]] = {}
_all_requests: Dict[int, ClarifyRequest] = {}
_notify_callbacks: Dict[str, object] = {}


def register(session_key: str, question: str, choices: Optional[List[str]]) -> ClarifyRequest:
    global _next_id
    with _lock:
        _next_id += 1
        req = ClarifyRequest(_next_id, session_key, question, list(choices) if choices else None,
                             awaiting_text=not bool(choices))
        queue = _requests.setdefault(session_key, [])
        was_empty = not queue
        queue.append(req)
        _all_requests[req.request_id] = req
        notify = _notify_callbacks.get(session_key) if was_empty else None
    if notify:
        try:
            notify(req)
        except Exception:
            logger.exception("Gateway clarify notification failed")
    return req


def register_gateway_notify(session_key: str, callback) -> None:
    """Register a callback invoked when a session's first prompt is queued."""
    with _lock:
        _notify_callbacks[session_key] = callback


def unregister_gateway_notify(session_key: str) -> None:
    """Remove a session notification callback."""
    with _lock:
        _notify_callbacks.pop(session_key, None)


def pending(session_key: str) -> Optional[ClarifyRequest]:
    with _lock:
        queue = _requests.get(session_key, [])
        return queue[0] if queue else None


def find(request_id: int) -> Optional[ClarifyRequest]:
    with _lock:
        return _all_requests.get(request_id)


def unregister(request_id: int) -> None:
    """Forget a completed/cancelled request after its waiter has returned."""
    with _lock:
        for key, queue in list(_requests.items()):
            for req in list(queue):
                if req.request_id == request_id:
                    queue.remove(req)
            if not queue:
                _requests.pop(key, None)
        _all_requests.pop(request_id, None)


def mark_awaiting_text(request_id: int) -> bool:
    with _lock:
        req = find(request_id)
        if not req or req.event.is_set():
            return False
        req.awaiting_text = True
        return True


def resolve(session_key: str, response: str) -> bool:
    with _lock:
        queue = _requests.get(session_key, [])
        if not queue:
            return False
        req = queue[0]
        if req.event.is_set():
            return False
        if req.choices and not req.awaiting_text:
            stripped = str(response).strip()
            if stripped.isdigit() and 1 <= int(stripped) <= len(req.choices):
                response = req.choices[int(stripped) - 1]
            elif stripped == str(len(req.choices) + 1):
                req.awaiting_text = True
                return False
            elif stripped not in req.choices:
                return False
        req.response = str(response)
        req.event.set()
        return True


def resolve_id(request_id: int, response: str) -> bool:
    with _lock:
        for key, queue in list(_requests.items()):
            for req in queue:
                if req.request_id == request_id:
                    if req.event.is_set():
                        return False
                    if req.choices and not req.awaiting_text:
                        stripped = str(response).strip()
                        if stripped.isdigit() and 1 <= int(stripped) <= len(req.choices):
                            response = req.choices[int(stripped) - 1]
                        elif stripped == str(len(req.choices) + 1):
                            req.awaiting_text = True
                            return False
                        elif stripped not in req.choices:
                            return False
                    req.response = str(response)
                    req.event.set()
                    return True
    return False


def resolve_choice(request_id: int, index: int) -> bool:
    """Complete a request with the exact choice selected by a button."""
    with _lock:
        req = _all_requests.get(request_id)
        if (
            not req or req.event.is_set() or req.awaiting_text
            or not req.choices or not 0 <= index < len(req.choices)
        ):
            return False
        req.response = req.choices[index]
        req.event.set()
        return True


def wait(req: ClarifyRequest, timeout: Optional[float]) -> str:
    deadline = None if timeout is None or timeout <= 0 else time.monotonic() + timeout
    while not req.event.wait(1.0):
        if deadline is not None and time.monotonic() >= deadline:
            with _lock:
                queue = _requests.get(req.session_key, [])
                if req in queue:
                    queue.remove(req)
                    if not queue:
                        _requests.pop(req.session_key, None)
                _all_requests.pop(req.request_id, None)
            return "[user did not respond before the clarify timeout]"
    try:
        if req.cancelled:
            return "[clarify cancelled because the session ended]"
        return req.response or ""
    finally:
        unregister(req.request_id)


def clear_session(session_key: str) -> int:
    with _lock:
        _notify_callbacks.pop(session_key, None)
        queue = _requests.pop(session_key, [])
        for req in queue:
            req.cancelled = True
            req.event.set()
        return len(queue)
