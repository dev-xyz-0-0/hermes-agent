"""Last-resort exception reporting for Hermes-owned background work.

Hooks are installed explicitly by process lifecycle code.  Importing this
module has no side effects.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from concurrent.futures import Future
from typing import Callable, Optional


logger = logging.getLogger(__name__)

_hook_lock = threading.RLock()
_previous_thread_hook: Optional[Callable] = None
_hermes_thread_hook: Optional[Callable] = None
_loop_handlers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Optional[Callable]]" = (
    weakref.WeakKeyDictionary()
)


def install_thread_exception_hook() -> bool:
    """Install the Hermes thread exception hook once.

    Returns ``True`` when this call installed the hook and ``False`` when it
    was already installed.
    """
    global _previous_thread_hook, _hermes_thread_hook

    with _hook_lock:
        if _hermes_thread_hook is not None and threading.excepthook is _hermes_thread_hook:
            return False

        previous = threading.excepthook

        def _thread_hook(args) -> None:
            try:
                if isinstance(args.exc_value, Exception):
                    thread_name = getattr(args.thread, "name", None) or "unknown"
                    logger.error(
                        "Unhandled exception in thread '%s'",
                        thread_name,
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                        extra={"hermes_component": "gateway"},
                    )
            except Exception:
                # Exception reporting must never replace the original failure.
                pass
            finally:
                try:
                    previous(args)
                except Exception:
                    pass

        _previous_thread_hook = previous
        _hermes_thread_hook = _thread_hook
        threading.excepthook = _thread_hook
        return True


def restore_thread_exception_hook() -> bool:
    """Restore the hook that preceded Hermes' hook, if still installed."""
    global _previous_thread_hook, _hermes_thread_hook

    with _hook_lock:
        if _hermes_thread_hook is None:
            return False
        if threading.excepthook is _hermes_thread_hook and _previous_thread_hook is not None:
            threading.excepthook = _previous_thread_hook
        _previous_thread_hook = None
        _hermes_thread_hook = None
        return True


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> bool:
    """Install a safe exception handler on an existing event loop."""
    if loop in _loop_handlers:
        return False

    previous = loop.get_exception_handler()

    def _asyncio_handler(active_loop, context) -> None:
        exc = context.get("exception")
        if isinstance(exc, asyncio.CancelledError):
            return

        message = str(context.get("message") or "Unhandled asyncio exception")
        task = context.get("task") or context.get("future")
        task_name = None
        if task is not None and hasattr(task, "get_name"):
            try:
                task_name = task.get_name()
            except Exception:
                task_name = None
        callback = context.get("handle") or context.get("callback")
        callback_text = repr(callback)[:200] if callback is not None else None

        try:
            logger.error(
                "Unhandled asyncio exception: message=%s exception_type=%s task=%s callback=%s",
                message,
                type(exc).__name__ if exc is not None else "None",
                task_name or "unknown",
                callback_text or "none",
                exc_info=(type(exc), exc, exc.__traceback__) if exc is not None else None,
                extra={"hermes_component": "gateway"},
            )
        except Exception:
            # Fall back to the loop's normal reporting if logging itself fails.
            active_loop.default_exception_handler(context)
            return

        # Preserve pre-existing application handlers.  Do not also invoke the
        # default handler: its asyncio logger would emit the same traceback to
        # the same root handlers a second time.
        if previous is not None:
            try:
                previous(active_loop, context)
            except Exception:
                logger.exception(
                    "Existing asyncio exception handler failed",
                    extra={"hermes_component": "gateway"},
                )

    _loop_handlers[loop] = previous
    loop.set_exception_handler(_asyncio_handler)
    return True


def restore_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> bool:
    """Restore a loop's prior exception handler."""
    if loop not in _loop_handlers:
        return False
    previous = _loop_handlers.pop(loop)
    if not loop.is_closed():
        loop.set_exception_handler(previous)
    return True


def observe_future(future: Future, *, component: str) -> Future:
    """Report an exception from an intentionally detached executor future."""

    def _report(done: Future) -> None:
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except Exception:
            logger.exception("Failed to inspect detached future: component=%s", component)
            return
        if exc is not None:
            logger.error(
                "Detached future failed: component=%s exception_type=%s",
                component,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    future.add_done_callback(_report)
    return future
