"""Tests for execution-scoped logging correlation."""

import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import patch

from cron import scheduler
from hermes_logging_context import (
    HermesLogContextFilter,
    cron_log_context,
    get_log_context,
)


def test_context_filter_defaults_and_reset_after_success_and_failure():
    context_filter = HermesLogContextFilter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    assert context_filter.filter(record)
    assert (record.job_id, record.run_id, record.profile) == ("-", "-", "-")
    assert record.hermes_context == ""

    try:
        with cron_log_context(job_id="job-1", run_id="run-1", profile="named"):
            assert get_log_context() == ("job-1", "run-1", "named")
            raise RuntimeError("reset me")
    except RuntimeError:
        pass

    assert get_log_context() == ("-", "-", "-")


def test_context_propagates_only_when_explicitly_copied_to_executor():
    with cron_log_context(job_id="job", run_id="run", profile="profile"):
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(context.run, get_log_context).result() == (
                "job", "run", "profile"
            )
            assert executor.submit(get_log_context).result() == ("-", "-", "-")


def test_tick_assigns_unique_run_ids_and_stable_context(caplog, tmp_path):
    job = {"id": "repeated-job", "name": "Repeated job"}
    seen = []

    def capture(stage):
        seen.append((stage, get_log_context()))

    caplog.handler.addFilter(HermesLogContextFilter())
    with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
         patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
         patch.object(scheduler, "get_due_jobs", return_value=[job]), \
         patch.object(scheduler, "advance_next_run", side_effect=lambda _job_id: capture("advance")), \
         patch.object(scheduler, "run_job", side_effect=lambda _job: (capture("run") or (True, "out", "done", None))), \
         patch.object(scheduler, "save_job_output", side_effect=lambda _job_id, _out: (capture("save") or tmp_path / "out")), \
         patch.object(scheduler, "_deliver_result", side_effect=lambda *_args, **_kwargs: (capture("delivery") or None)), \
         patch.object(scheduler, "mark_job_run", side_effect=lambda *_args, **_kwargs: capture("mark")), \
         patch("hermes_cli.profiles.get_active_profile_name", return_value="profile-a"):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            assert scheduler.tick(verbose=False) == 1
            assert scheduler.tick(verbose=False) == 1

    assert get_log_context() == ("-", "-", "-")
    contexts = [context for _, context in seen]
    assert all(job_id == "repeated-job" and profile == "profile-a" for job_id, _, profile in contexts)
    run_ids = [context[1] for stage, context in seen if stage == "advance"]
    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]
    for run_id in run_ids:
        assert all(
            context[1] == run_id
            for _, context in seen[run_ids.index(run_id) * 5:(run_ids.index(run_id) + 1) * 5]
        )

    lifecycle = [record for record in caplog.records if record.message.startswith("cron.")]
    assert lifecycle
    assert {record.job_id for record in lifecycle} == {"repeated-job"}
    assert {record.profile for record in lifecycle} == {"profile-a"}
