"""Per-draft and per-destination locking for publication (Phase 3).

``agent/skill_creation/registry.py``'s atomic manifest replace prevents a
*corrupted* manifest, but not two processes concurrently approving/
publishing the *same* draft, or two different drafts racing to publish to
the *same* destination path. This module adds that exclusion.

Reimplements ``gateway/status.py``'s proven PID+start-time staleness check
(dead or stopped lock holder) rather than reusing ``acquire_scoped_lock``
directly -- that function's lock directory is machine-wide
(``XDG_STATE_HOME``), the wrong scope for a draft lock that should live
alongside the draft itself and be naturally profile-scoped. The one tricky
bit (reading a process's kernel start time from ``/proc``) is imported
directly from ``gateway.status`` rather than duplicated.

Ownership and timeout semantics:
  - A lock is owned by exactly one (pid, thread_id, start_time) triple.
  - Nested acquisition -- the same thread calling ``draft_lock``/
    ``destination_lock`` again while it already holds that lock -- is
    **explicitly rejected** (raises ``DraftLockError`` immediately, not
    after waiting out the timeout). No call site in this package ever
    nests a lock; reference-counted reentrancy would add complexity for a
    case that should never happen, so an attempt is treated as a bug in
    the caller rather than silently allowed (which would otherwise let two
    concurrent operations on the same thread's call stack both believe
    they hold exclusive access).
  - A lock held by a dead or stopped process (or, for the same PID, a
    *different* thread) is waited on via `timeout`-bounded polling, then
    raises ``DraftLockError`` -- it is never force-stolen while the holder
    is actually alive, only reclaimed once ``_is_stale`` confirms the
    holder PID no longer exists or has a different kernel start time than
    recorded (i.e. the PID was reused by an unrelated process).
  - ``finally: _release(...)`` always runs on context-manager exit
    (including on an exception raised inside the ``with`` block), and
    ``_release`` only ever removes a lock file it can verify (by pid +
    thread_id) belongs to the caller -- it never removes a lock another
    holder (sibling thread, other process, or a stale-reclaim winner)
    currently owns.
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

_POLL_INTERVAL = 0.05


class DraftLockError(Exception):
    """Raised when a lock can't be acquired within the timeout."""


def _build_record() -> dict:
    import threading as _threading
    from gateway.status import _get_process_start_time

    return {
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        # Two threads in the same process share a PID -- without this, the
        # "already held by this process" fast path below would let a second
        # thread barge in on a lock its sibling thread legitimately holds.
        "thread_id": _threading.get_ident(),
    }


def _is_stale(record: dict) -> bool:
    from gateway.status import _get_process_start_time

    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return True

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        pass  # treat as alive if the check itself is inconclusive

    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and current_start != recorded_start:
        return True
    return False


def _try_acquire(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = _build_record()

    existing_raw = None
    if lock_path.exists():
        try:
            existing_raw = json.loads(lock_path.read_text())
        except (OSError, json.JSONDecodeError):
            existing_raw = None

    if existing_raw is not None:
        if (
            existing_raw.get("pid") == record["pid"]
            and existing_raw.get("start_time") == record["start_time"]
            and existing_raw.get("thread_id") == record["thread_id"]
        ):
            # Nested acquisition by the exact same thread that already
            # holds this lock -- explicitly rejected (see module docstring:
            # no call site needs this, and silently returning True here
            # would let two "concurrent" operations on one call stack both
            # believe they hold exclusive access).
            raise DraftLockError(
                f"Nested acquisition of {lock_path} is not supported -- "
                "this thread already holds it."
            )
        if not _is_stale(existing_raw):
            return False
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def _release(lock_path: Path) -> None:
    import threading as _threading

    try:
        existing = json.loads(lock_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if existing.get("pid") != os.getpid() or existing.get("thread_id") != _threading.get_ident():
        return  # not ours (e.g. a sibling thread's lock, or already reclaimed as stale)
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def _file_lock(lock_path: Path, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    acquired = _try_acquire(lock_path)
    while not acquired:
        if time.monotonic() >= deadline:
            raise DraftLockError(f"Timed out waiting for lock on {what} (held by another process).")
        time.sleep(_POLL_INTERVAL)
        acquired = _try_acquire(lock_path)
    try:
        yield
    finally:
        _release(lock_path)


@contextmanager
def draft_lock(draft_id: str, timeout: float = 10.0):
    """Exclude other processes from operating on this draft_id concurrently."""
    from agent.skill_creation import registry

    lock_path = registry.get_drafts_root() / draft_id / ".lock"
    with _file_lock(lock_path, timeout, f"draft '{draft_id}'"):
        yield


@contextmanager
def destination_lock(destination: Path, timeout: float = 10.0):
    """Exclude other drafts from publishing to the same destination path.

    The lock file is a sibling of the destination (``<destination>.publish.lock``),
    not inside it -- the destination itself must not exist yet when publishing
    a brand-new skill.
    """
    lock_path = destination.parent / f".{destination.name}.publish.lock"
    with _file_lock(lock_path, timeout, f"destination '{destination}'"):
        yield
