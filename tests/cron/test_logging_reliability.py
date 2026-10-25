"""Cron logging reliability tests."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.redact import RedactingFormatter
from cron import scheduler
from hermes_logging_context import HermesLogContextFilter


def _install_context_filter(caplog, request):
    context_filter = HermesLogContextFilter()
    caplog.handler.addFilter(context_filter)
    request.addfinalizer(
        lambda: caplog.handler.removeFilter(context_filter)
    )


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


def test_scheduler_processing_failure_logs_traceback_and_preserves_status(
    caplog, request, tmp_path
):
    job = {"id": "safe-job-id", "name": "Synthetic job"}
    _install_context_filter(caplog, request)

    with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
         patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
         patch.object(scheduler, "get_due_jobs", return_value=[job]), \
         patch.object(scheduler, "advance_next_run", side_effect=ValueError("synthetic processing failure")), \
         patch.object(scheduler, "mark_job_run") as mark:
        with caplog.at_level(logging.ERROR, logger="cron.scheduler"):
            assert scheduler.tick(verbose=False) == 0

    record = next(r for r in caplog.records if r.message == "cron.execution.failed")
    assert record.exc_info[0] is ValueError
    assert record.job_id == "safe-job-id"
    assert record.run_id
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


def test_post_execution_persistence_failure_does_not_duplicate_terminal_event(
    caplog, request, tmp_path
):
    job = {"id": "post-processing-job", "name": "Synthetic job"}
    _install_context_filter(caplog, request)

    with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
         patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
         patch.object(scheduler, "get_due_jobs", return_value=[job]), \
         patch.object(scheduler, "advance_next_run"), \
         patch.object(
             scheduler,
             "run_job",
             return_value=(True, "output", "", None),
         ), \
         patch.object(
             scheduler,
             "save_job_output",
             side_effect=OSError("output write failed"),
         ), \
         patch.object(scheduler, "mark_job_run"):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            assert scheduler.tick(verbose=False) == 0

    lifecycle = [
        record.message for record in caplog.records
        if record.message.startswith("cron.execution.")
    ]
    assert lifecycle == ["cron.execution.started", "cron.execution.succeeded"]
    processing = next(
        record for record in caplog.records
        if record.message == "Cron job post-processing failed"
    )
    assert processing.exc_info[0] is OSError


def test_delivery_lifecycle_uses_exact_terminal_event_names(
    caplog, request, tmp_path
):
    jobs = [
        {"id": "returned-delivery-error", "name": "Returned error"},
        {"id": "raised-delivery-error", "name": "Raised error"},
    ]
    _install_context_filter(caplog, request)

    def deliver(job, *_args, **_kwargs):
        if job["id"] == "returned-delivery-error":
            return "synthetic returned error"
        raise RuntimeError("synthetic raised error")

    with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
         patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
         patch.object(scheduler, "get_due_jobs", return_value=jobs), \
         patch.object(scheduler, "advance_next_run"), \
         patch.object(
             scheduler,
             "run_job",
             return_value=(True, "output", "deliver me", None),
         ), \
         patch.object(scheduler, "save_job_output", return_value=tmp_path / "out"), \
         patch.object(scheduler, "_deliver_result", side_effect=deliver), \
         patch.object(scheduler, "mark_job_run"), \
         patch(
             "hermes_cli.profiles.get_active_profile_name",
             return_value="isolated",
         ):
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            assert scheduler.tick(verbose=False) == 2

    by_run = {}
    for record in caplog.records:
        if record.message.startswith("cron.delivery."):
            by_run.setdefault(record.run_id, []).append(record.message)
    assert len(by_run) == 2
    assert all(
        messages == ["cron.delivery.started", "cron.delivery.failed"]
        for messages in by_run.values()
    )


def test_correlated_cron_traceback_redacts_secret_in_persisted_output(tmp_path):
    path = tmp_path / "cron-errors.log"
    handler = logging.FileHandler(path)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(
        RedactingFormatter("%(levelname)s %(hermes_context)s%(message)s")
    )
    handler.addFilter(HermesLogContextFilter())
    cron_logger = logging.getLogger("cron.scheduler")
    cron_logger.addHandler(handler)
    job = {"id": "redaction-job", "name": "Synthetic job"}
    try:
        with patch.object(scheduler, "_LOCK_DIR", tmp_path), \
             patch.object(scheduler, "_LOCK_FILE", tmp_path / ".tick.lock"), \
             patch.object(scheduler, "get_due_jobs", return_value=[job]), \
             patch.object(
                 scheduler,
                 "advance_next_run",
                 side_effect=RuntimeError(
                     "https://example.test/?token=cron-secret-value"
                 ),
             ), \
             patch.object(scheduler, "mark_job_run"):
            scheduler.tick(verbose=False)
        handler.flush()
        content = path.read_text()
        assert "Traceback" in content
        assert "RuntimeError" in content
        assert "job_id=redaction-job" in content
        assert "cron-secret-value" not in content
    finally:
        cron_logger.removeHandler(handler)
        handler.close()


def test_lifecycle_logging_survives_agent_backed_run_and_reconfiguration(
    caplog, tmp_path
):
    """A quiet cron agent must not disable lifecycle logs for later ticks."""
    import hermes_logging
    import run_agent
    from run_agent import AIAgent

    hermes_home = tmp_path / "profile-home"
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_root_level = root.level
    original_initialized = hermes_logging._logging_initialized
    logger_names = (
        "cron",
        "cron.scheduler",
        "tools",
        "run_agent",
        "trajectory_compressor",
        "hermes_cli",
    )
    original_levels = {
        name: logging.getLogger(name).level for name in logger_names
    }
    context_filter = HermesLogContextFilter()
    caplog.handler.addFilter(context_filter)

    # Temporarily detach any managed file handlers without closing them. This
    # prevents forced profile switching from closing handlers owned by another
    # test or by pytest's process-level logging setup.
    detached_handlers = [
        handler for handler in original_handlers
        if getattr(handler, "_hermes_managed", False)
        and getattr(handler, "_hermes_log_kind", None) in {"agent", "errors"}
    ]
    for handler in detached_handlers:
        root.removeHandler(handler)

    jobs = [
        {"id": "script-before", "name": "script-before"},
        {"id": "agent-middle", "name": "agent-middle", "prompt": "synthetic"},
        {"id": "script-after", "name": "script-after"},
        {"id": "execution-failed", "name": "execution-failed"},
        {"id": "delivery-success", "name": "delivery-success"},
        {"id": "delivery-failed", "name": "delivery-failed"},
    ]
    real_run_job = scheduler.run_job

    def run_synthetic_job(job):
        if job["id"] == "execution-failed":
            return False, "# failed", "", "synthetic execution failure"
        if job["id"].startswith("delivery-"):
            return True, "# synthetic", "deliver me", None
        if job["id"] != "agent-middle":
            return True, "# synthetic", "", None

        runtime = {
            "api_key": "synthetic-key",
            "base_url": "https://example.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
        }
        route = {"model": "test/model", "runtime": runtime}
        with patch.object(run_agent, "_hermes_home", hermes_home), \
             patch.object(scheduler, "_hermes_home", hermes_home), \
             patch("run_agent.get_tool_definitions", return_value=[]), \
             patch("run_agent.check_toolset_requirements", return_value={}), \
             patch("run_agent.fetch_model_metadata", return_value={}), \
             patch(
                 "agent.context_compressor.get_model_context_length",
                 return_value=128_000,
             ), \
             patch("run_agent.OpenAI"), \
             patch.object(
                 AIAgent,
                 "run_conversation",
                 return_value={"final_response": ""},
             ), \
             patch("hermes_state.SessionDB", return_value=MagicMock()), \
             patch("dotenv.load_dotenv"), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value=runtime,
             ), \
             patch(
                 "agent.smart_model_routing.resolve_turn_route",
                 return_value=route,
             ):
            return real_run_job(job)

    try:
        hermes_logging._logging_initialized = False
        for name in logger_names:
            logging.getLogger(name).setLevel(logging.NOTSET)
        root.setLevel(logging.INFO)
        caplog.handler.setLevel(logging.INFO)
        hermes_logging.setup_logging(
            hermes_home=hermes_home,
            mode="gateway",
            force=True,
        )
        from agent.redact import RedactingFormatter
        from hermes_logging import _LOG_FORMAT, _add_rotating_handler
        from hermes_logging_context import GatewayLogFilter, add_filter_once

        gateway_handler = _add_rotating_handler(
            root,
            hermes_home / "logs" / "gateway.log",
            level=logging.INFO,
            max_bytes=1024 * 1024,
            backup_count=1,
            formatter=RedactingFormatter(_LOG_FORMAT),
            managed_kind="gateway",
            reconfigure=True,
        )
        add_filter_once(gateway_handler, HermesLogContextFilter)
        add_filter_once(gateway_handler, GatewayLogFilter)

        def deliver(job, *_args, **_kwargs):
            if job["id"] == "delivery-failed":
                return "synthetic delivery failure"
            return None

        with patch.dict(
                 os.environ,
                 {"HOME": str(tmp_path), "HERMES_HOME": str(hermes_home)},
             ), \
             patch.object(scheduler, "_LOCK_DIR", hermes_home / "cron"), \
             patch.object(
                 scheduler,
                 "_LOCK_FILE",
                 hermes_home / "cron" / ".tick.lock",
             ), \
             patch.object(
                 scheduler,
                 "get_due_jobs",
                 side_effect=[[job] for job in jobs],
             ), \
             patch.object(scheduler, "advance_next_run"), \
             patch.object(scheduler, "run_job", side_effect=run_synthetic_job), \
             patch.object(
                 scheduler,
                 "save_job_output",
                 return_value=hermes_home / "out.md",
             ), \
             patch.object(scheduler, "_deliver_result", side_effect=deliver), \
             patch.object(scheduler, "mark_job_run"), \
             patch(
                 "hermes_cli.profiles.get_active_profile_name",
                 return_value="isolated",
             ):
            for index in range(len(jobs)):
                assert scheduler.tick(verbose=False) == 1
                if index == 1:
                    hermes_logging.setup_logging(
                        hermes_home=hermes_home,
                        force=True,
                    )

        lifecycle = [
            record for record in caplog.records
            if record.message in {
                "cron.execution.started",
                "cron.execution.succeeded",
                "cron.execution.failed",
            }
        ]
        assert [record.message for record in lifecycle].count(
            "cron.execution.started"
        ) == 6
        assert [record.message for record in lifecycle].count(
            "cron.execution.succeeded"
        ) == 5
        assert [record.message for record in lifecycle].count(
            "cron.execution.failed"
        ) == 1
        assert {record.profile for record in lifecycle} == {"isolated"}

        by_run = {}
        for record in lifecycle:
            by_run.setdefault(record.run_id, []).append(record)
        assert len(by_run) == 6
        assert all(
            len(records) == 2
            and records[0].message == "cron.execution.started"
            and records[1].message
            in {"cron.execution.succeeded", "cron.execution.failed"}
            for records in by_run.values()
        )
        assert all(
            len({record.job_id for record in records}) == 1
            and len({record.profile for record in records}) == 1
            for records in by_run.values()
        )
        delivery_by_run = {}
        for record in caplog.records:
            if record.message.startswith("cron.delivery."):
                delivery_by_run.setdefault(record.run_id, []).append(record)
        assert len(delivery_by_run) == 3
        assert all(
            len(records) == 2
            and records[0].message == "cron.delivery.started"
            and records[1].message
            in {"cron.delivery.succeeded", "cron.delivery.failed"}
            and len({record.job_id for record in records}) == 1
            and len({record.profile for record in records}) == 1
            for records in delivery_by_run.values()
        )

        for handler in root.handlers:
            handler.flush()
        content = (hermes_home / "logs" / "agent.log").read_text()
        assert content.count("cron.execution.started") == 6
        assert content.count("cron.execution.succeeded") == 5
        assert content.count("cron.execution.failed") == 1
        assert content.count("cron.delivery.started") == 3
        assert content.count("cron.delivery.succeeded") == 2
        assert content.count("cron.delivery.failed") == 1
        errors_content = (hermes_home / "logs" / "errors.log").read_text()
        assert "cron.execution.failed" in errors_content
        assert "cron.delivery.failed" in errors_content
        assert "cron.execution." not in (
            hermes_home / "logs" / "gateway.log"
        ).read_text()
    finally:
        caplog.handler.removeFilter(context_filter)
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in detached_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(original_root_level)
        for name, level in original_levels.items():
            logging.getLogger(name).setLevel(level)
        hermes_logging._logging_initialized = original_initialized
