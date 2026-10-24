"""Cron logging reliability tests."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from cron import scheduler


def test_standalone_scheduler_initializes_profile_logging(tmp_path):
    hermes_home = tmp_path / "profile-home"
    env = dict(os.environ, HERMES_HOME=str(hermes_home))

    result = subprocess.run(
        [sys.executable, "-m", "cron.scheduler"],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "logs" / "agent.log").exists()
    assert (hermes_home / "logs" / "errors.log").exists()
    assert "No jobs due" in (hermes_home / "logs" / "agent.log").read_text()


def test_two_processes_can_append_without_rotation(tmp_path):
    hermes_home = tmp_path / "shared-profile"
    env = dict(os.environ, HERMES_HOME=str(hermes_home))
    code = (
        "import logging; "
        "from hermes_logging import setup_logging; "
        "setup_logging(mode='cron'); "
        "logging.getLogger('cron.concurrent').warning(%r)"
    )

    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code % marker],
            cwd=Path(__file__).parents[2],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for marker in ("concurrent-record-one", "concurrent-record-two")
    ]
    results = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
    assert all(returncode == 0 for _, _, returncode in results), results

    content = (hermes_home / "logs" / "errors.log").read_text()
    assert "concurrent-record-one" in content
    assert "concurrent-record-two" in content


def test_scheduler_processing_failure_logs_traceback_and_preserves_status(caplog, tmp_path):
    job = {"id": "safe-job-id", "name": "Synthetic job"}

    with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
         patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
         patch.object(scheduler, "get_due_jobs", return_value=[job]), \
         patch.object(scheduler, "advance_next_run", side_effect=ValueError("synthetic processing failure")), \
         patch.object(scheduler, "mark_job_run") as mark:
        with caplog.at_level(logging.ERROR, logger="cron.scheduler"):
            assert scheduler.tick(verbose=False) == 0

    record = next(r for r in caplog.records if "Cron job processing failed" in r.message)
    assert "safe-job-id" in record.message
    assert record.exc_info[0] is ValueError
    mark.assert_called_once_with("safe-job-id", False, "synthetic processing failure")


def test_status_persistence_failure_does_not_hide_original_traceback(caplog, tmp_path):
    job = {"id": "status-job-id", "name": "Synthetic job"}

    with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
         patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
         patch.object(scheduler, "get_due_jobs", return_value=[job]), \
         patch.object(scheduler, "run_job", side_effect=RuntimeError("original failure")), \
         patch.object(scheduler, "advance_next_run"), \
         patch.object(scheduler, "mark_job_run", side_effect=OSError("status write failed")):
        with caplog.at_level(logging.ERROR, logger="cron.scheduler"):
            assert scheduler.tick(verbose=False) == 0

    records = [r for r in caplog.records if r.exc_info]
    assert any(r.exc_info[0] is RuntimeError for r in records)
    assert any(r.exc_info[0] is OSError for r in records)
