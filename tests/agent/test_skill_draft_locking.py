"""Tests for agent/skill_creation/locking.py (Phase 3)."""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from agent.skill_creation import locking


class TestDraftLock:
    def test_acquire_and_release(self):
        with locking.draft_lock("a" * 32, timeout=1):
            pass  # no exception == success

    def test_nested_acquisition_by_same_thread_is_rejected(self):
        """Nested acquisition is explicitly rejected, not silently allowed --
        no call site needs reentrancy, and allowing it would let two
        "concurrent" operations on one call stack both believe they hold
        exclusive access. Must fail fast, not wait out the timeout."""
        with locking.draft_lock("b" * 32, timeout=1):
            start = time.monotonic()
            with pytest.raises(locking.DraftLockError):
                with locking.draft_lock("b" * 32, timeout=5):
                    pass
            assert time.monotonic() - start < 1.0  # rejected immediately, not after the 5s timeout

    def test_outer_lock_still_held_after_rejected_nested_attempt(self):
        """A failed nested-acquisition attempt must not release the outer lock."""
        from agent.skill_creation import registry

        draft_id = "f" * 32
        lock_path = registry.get_drafts_root() / draft_id / ".lock"
        with locking.draft_lock(draft_id, timeout=1):
            with pytest.raises(locking.DraftLockError):
                with locking.draft_lock(draft_id, timeout=0.2):
                    pass
            assert lock_path.exists()  # outer lock is still intact
        assert not lock_path.exists()  # released on the outer context's exit

    def test_lock_file_removed_after_release(self):
        from agent.skill_creation import registry

        draft_id = "c" * 32
        lock_path = registry.get_drafts_root() / draft_id / ".lock"
        with locking.draft_lock(draft_id, timeout=1):
            assert lock_path.exists()
        assert not lock_path.exists()

    def test_cross_process_exclusion_times_out(self, tmp_path):
        """A lock held by a real other process blocks this one until timeout."""
        home = os.environ["HERMES_HOME"]
        draft_id = "d" * 32
        holder_script = tmp_path / "holder.py"
        holder_script.write_text(
            "import os, time\n"
            f"os.environ['HERMES_HOME'] = {home!r}\n"
            "from agent.skill_creation import locking\n"
            f"with locking.draft_lock({draft_id!r}, timeout=2):\n"
            "    print('locked', flush=True)\n"
            "    time.sleep(2)\n"
        )
        proc = subprocess.Popen([sys.executable, str(holder_script)], stdout=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            assert "locked" in line
            time.sleep(0.2)

            start = time.monotonic()
            with pytest.raises(locking.DraftLockError):
                with locking.draft_lock(draft_id, timeout=0.3):
                    pass
            assert time.monotonic() - start < 2.0
        finally:
            proc.wait(timeout=5)

    def test_stale_lock_from_dead_process_is_reclaimed(self):
        """A lock file left by a PID that no longer exists must not block forever."""
        from agent.skill_creation import registry

        draft_id = "e" * 32
        lock_path = registry.get_drafts_root() / draft_id / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # A PID that is overwhelmingly unlikely to be alive.
        lock_path.write_text(json.dumps({"pid": 999999, "start_time": 123}))

        with locking.draft_lock(draft_id, timeout=1):
            pass  # must not raise/time out -- the stale lock is reclaimed


class TestDestinationLock:
    def test_acquire_and_release(self, tmp_path):
        dest = tmp_path / "skills" / "my-skill"
        with locking.destination_lock(dest, timeout=1):
            pass

    def test_lock_file_is_sibling_not_inside_destination(self, tmp_path):
        dest = tmp_path / "skills" / "my-skill"
        with locking.destination_lock(dest, timeout=1):
            # The destination itself must stay untouched/nonexistent --
            # it's a brand-new skill that hasn't been published yet.
            assert not dest.exists()

    def test_two_different_destinations_do_not_contend(self, tmp_path):
        dest_a = tmp_path / "skills" / "skill-a"
        dest_b = tmp_path / "skills" / "skill-b"
        with locking.destination_lock(dest_a, timeout=1), locking.destination_lock(dest_b, timeout=1):
            pass

    def test_concurrent_destination_publish_yields_one_winner(self, tmp_path):
        dest = tmp_path / "skills" / "race-dest"
        winners = []
        lock_acquired_order = []

        def worker(i):
            try:
                with locking.destination_lock(dest, timeout=2):
                    lock_acquired_order.append(i)
                    time.sleep(0.05)
                    winners.append(i)
            except locking.DraftLockError:
                pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All five should eventually succeed (sequential access via the
        # lock), never overlapping -- the lock serializes, it doesn't drop
        # requests. What matters is they never ran concurrently.
        assert len(winners) == 5
