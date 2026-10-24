"""Tests for hermes_logging — centralized logging setup."""

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

import hermes_logging
from agent.redact import RedactingFormatter
from hermes_logging_context import HermesLogContextFilter


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset the module-level sentinel and clean up root logger handlers
    added by setup_logging() so tests don't leak state.

    Under xdist (-n auto) other test modules may have called setup_logging()
    in the same worker process, leaving RotatingFileHandlers on the root
    logger.  We strip ALL RotatingFileHandlers before each test so the count
    assertions are stable regardless of test ordering.
    """
    hermes_logging._logging_initialized = False
    root = logging.getLogger()
    # Strip ALL RotatingFileHandlers — not just the ones we added — so that
    # handlers leaked from other test modules in the same xdist worker don't
    # pollute our counts.
    pre_existing = []
    for h in list(root.handlers):
        if isinstance(h, RotatingFileHandler):
            root.removeHandler(h)
            h.close()
        else:
            pre_existing.append(h)
    yield
    # Restore — remove any handlers added during the test.
    for h in list(root.handlers):
        if h not in pre_existing:
            root.removeHandler(h)
            h.close()
    hermes_logging._logging_initialized = False


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Provide an isolated HERMES_HOME for logging tests.

    Uses the same tmp_path as the autouse _isolate_hermes_home from conftest,
    reading it back from the env var to avoid double-mkdir conflicts.
    """
    home = Path(os.environ["HERMES_HOME"])
    return home


class TestSetupLogging:
    """setup_logging() creates agent.log + errors.log with RotatingFileHandler."""

    def test_creates_log_directory(self, hermes_home):
        log_dir = hermes_logging.setup_logging(hermes_home=hermes_home)
        assert log_dir == hermes_home / "logs"
        assert log_dir.is_dir()

    def test_creates_agent_log_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()

        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1
        assert agent_handlers[0].level == logging.INFO

    def test_creates_errors_log_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()

        error_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "errors.log" in getattr(h, "baseFilename", "")
        ]
        assert len(error_handlers) == 1
        assert error_handlers[0].level == logging.WARNING

    def test_idempotent_no_duplicate_handlers(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_logging(hermes_home=hermes_home)  # second call — should be no-op

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1

    def test_force_reinitializes(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        # Force still won't add duplicate handlers because _add_rotating_handler
        # checks by resolved path.
        hermes_logging.setup_logging(hermes_home=hermes_home, force=True)

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1

    def test_force_updates_existing_handler_in_place(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )

        hermes_logging.setup_logging(
            hermes_home=hermes_home,
            log_level="DEBUG",
            max_size_mb=10,
            backup_count=5,
            force=True,
        )

        configured = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        assert configured is handler
        assert configured.level == logging.DEBUG
        assert configured.maxBytes == 10 * 1024 * 1024
        assert configured.backupCount == 5

    def test_in_place_reconfiguration_preserves_context_filter(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        context_filter = next(
            item for item in handler.filters
            if isinstance(item, HermesLogContextFilter)
        )

        hermes_logging.setup_logging(
            hermes_home=hermes_home, max_size_mb=9, force=True
        )

        assert handler in logging.getLogger().handlers
        assert context_filter in handler.filters
        assert sum(
            isinstance(item, HermesLogContextFilter)
            for item in handler.filters
        ) == 1

    def test_recovers_managed_handler_removed_externally(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()
        errors = next(
            h for h in root.handlers
            if getattr(h, "_hermes_log_kind", None) == "errors"
        )
        root.removeHandler(errors)
        errors.close()

        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert len([
            h for h in root.handlers
            if getattr(h, "_hermes_log_kind", None) == "errors"
        ]) == 1

    def test_force_switches_profiles_and_closes_previous_handlers(self, tmp_path):
        first_home = tmp_path / "first"
        second_home = tmp_path / "second"
        hermes_logging.setup_logging(hermes_home=first_home)
        root = logging.getLogger()
        old = {
            getattr(h, "_hermes_log_kind", None): h
            for h in root.handlers
            if getattr(h, "_hermes_log_kind", None) in {"agent", "errors"}
        }

        result = hermes_logging.setup_logging(
            hermes_home=second_home, force=True
        )

        assert result == second_home / "logs"
        current = {
            getattr(h, "_hermes_log_kind", None): h
            for h in root.handlers
            if getattr(h, "_hermes_log_kind", None) in {"agent", "errors"}
        }
        assert all(Path(h.baseFilename).parent == second_home / "logs" for h in current.values())
        assert all(h not in root.handlers and h.stream is None for h in old.values())
        assert all(
            sum(isinstance(item, HermesLogContextFilter) for item in h.filters) == 1
            for h in current.values()
        )

    def test_failed_profile_switch_keeps_previous_handlers(
        self, tmp_path, monkeypatch
    ):
        first_home = tmp_path / "first"
        second_home = tmp_path / "second"
        hermes_logging.setup_logging(hermes_home=first_home)
        root = logging.getLogger()
        old = {
            getattr(h, "_hermes_log_kind", None): h
            for h in root.handlers
            if getattr(h, "_hermes_log_kind", None) in {"agent", "errors"}
        }
        real_create = hermes_logging._create_rotating_handler
        created = []

        def fail_second(**kwargs):
            if kwargs["managed_kind"] == "errors":
                raise OSError("synthetic handler creation failure")
            handler = real_create(**kwargs)
            created.append(handler)
            return handler

        monkeypatch.setattr(hermes_logging, "_create_rotating_handler", fail_second)
        with pytest.raises(OSError, match="synthetic handler"):
            hermes_logging.setup_logging(hermes_home=second_home, force=True)

        assert all(handler in root.handlers for handler in old.values())
        assert all(handler.stream is not None for handler in old.values())
        assert created and all(handler.stream is None for handler in created)

    def test_attachment_failure_rolls_back_entire_profile_switch(
        self, tmp_path, monkeypatch
    ):
        first_home = tmp_path / "first"
        second_home = tmp_path / "second"
        hermes_logging.setup_logging(hermes_home=first_home)
        root = logging.getLogger()
        old_handlers = [
            h for h in root.handlers
            if getattr(h, "_hermes_log_kind", None) in {"agent", "errors"}
        ]
        prepared = []

        def attach_then_fail(handler):
            prepared.append(handler)
            root.handlers.append(handler)
            if len(prepared) == 2:
                raise RuntimeError("synthetic attachment failure")

        monkeypatch.setattr(root, "addHandler", attach_then_fail)
        with pytest.raises(RuntimeError, match="attachment failure"):
            hermes_logging.setup_logging(hermes_home=second_home, force=True)

        assert all(handler in root.handlers for handler in old_handlers)
        assert all(handler.stream is not None for handler in old_handlers)
        assert len(prepared) == 2
        assert all(handler not in root.handlers for handler in prepared)
        assert all(handler.stream is None for handler in prepared)

    def test_recognizable_legacy_handler_is_adopted(self, hermes_home):
        root = logging.getLogger()
        path = hermes_home / "logs" / "agent.log"
        path.parent.mkdir(parents=True)
        legacy = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        root.addHandler(legacy)

        hermes_logging.setup_logging(hermes_home=hermes_home)

        matching = [
            handler for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename).resolve() == path.resolve()
        ]
        assert matching == [legacy]
        assert legacy._hermes_managed is True
        assert legacy._hermes_log_kind == "agent"
        assert isinstance(legacy.formatter, RedactingFormatter)
        assert any(isinstance(item, HermesLogContextFilter) for item in legacy.filters)

    def test_ambiguous_same_path_handler_remains_untouched(self, hermes_home):
        root = logging.getLogger()
        path = hermes_home / "logs" / "agent.log"
        path.parent.mkdir(parents=True)
        formatter = logging.Formatter("ambiguous:%(message)s")
        ambiguous = RotatingFileHandler(path, maxBytes=1234, backupCount=7)
        ambiguous.setLevel(logging.ERROR)
        ambiguous.setFormatter(formatter)
        root.addHandler(ambiguous)

        hermes_logging.setup_logging(hermes_home=hermes_home)

        managed = next(
            handler for handler in root.handlers
            if getattr(handler, "_hermes_log_kind", None) == "agent"
        )
        assert managed is not ambiguous
        assert ambiguous in root.handlers
        assert not getattr(ambiguous, "_hermes_managed", False)
        assert ambiguous.level == logging.ERROR
        assert ambiguous.maxBytes == 1234
        assert ambiguous.backupCount == 7
        assert ambiguous.formatter is formatter

    def test_force_preserves_unrelated_and_verbose_handlers(self, hermes_home):
        root = logging.getLogger()
        unrelated = logging.StreamHandler()
        root.addHandler(unrelated)
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_verbose_logging()
        verbose = next(h for h in root.handlers if getattr(h, "_hermes_verbose", False))

        hermes_logging.setup_logging(
            hermes_home=hermes_home, log_level="WARNING", force=True
        )

        assert unrelated in root.handlers
        assert verbose in root.handlers
        assert root.level <= logging.DEBUG

    def test_setup_preserves_notset_root_for_unrelated_debug_handlers(
        self, hermes_home
    ):
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.NOTSET)
        try:
            hermes_logging.setup_logging(hermes_home=hermes_home)
            assert root.level == logging.NOTSET
        finally:
            root.setLevel(previous_level)

    def test_concurrent_initialization_creates_one_handler_per_kind(self, hermes_home):
        workers = 6
        barrier = threading.Barrier(workers)
        failures = []

        def initialize():
            try:
                barrier.wait(timeout=5)
                hermes_logging.setup_logging(hermes_home=hermes_home, force=True)
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=initialize) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not failures
        assert not any(thread.is_alive() for thread in threads)
        kinds = [
            getattr(h, "_hermes_log_kind", None)
            for h in logging.getLogger().handlers
        ]
        assert kinds.count("agent") == 1
        assert kinds.count("errors") == 1

    def test_custom_log_level(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, log_level="DEBUG")

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.DEBUG

    def test_custom_max_size_and_backup(self, hermes_home):
        hermes_logging.setup_logging(
            hermes_home=hermes_home, max_size_mb=10, backup_count=5
        )

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].maxBytes == 10 * 1024 * 1024
        assert agent_handlers[0].backupCount == 5

    def test_suppresses_noisy_loggers(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert logging.getLogger("openai").level >= logging.WARNING
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

    def test_writes_to_agent_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.write_test")
        test_logger.info("test message for agent.log")

        # Flush handlers
        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = hermes_home / "logs" / "agent.log"
        assert agent_log.exists()
        content = agent_log.read_text()
        assert "test message for agent.log" in content

    def test_warnings_appear_in_both_logs(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.warning_test")
        test_logger.warning("this is a warning")

        for h in logging.getLogger().handlers:
            h.flush()

        agent_log = hermes_home / "logs" / "agent.log"
        errors_log = hermes_home / "logs" / "errors.log"
        assert "this is a warning" in agent_log.read_text()
        assert "this is a warning" in errors_log.read_text()

    def test_info_not_in_errors_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.info_test")
        test_logger.info("info only message")

        for h in logging.getLogger().handlers:
            h.flush()

        errors_log = hermes_home / "logs" / "errors.log"
        if errors_log.exists():
            assert "info only message" not in errors_log.read_text()

    def test_redacts_secret_from_exception_traceback(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        test_logger = logging.getLogger("test_hermes_logging.redaction")

        try:
            raise RuntimeError("failed with sk-ant-api03-THIS_IS_A_SYNTHETIC_SECRET_123456")
        except RuntimeError:
            test_logger.exception("synthetic provider failure")

        for handler in logging.getLogger().handlers:
            handler.flush()

        content = (hermes_home / "logs" / "errors.log").read_text()
        assert "Traceback" in content
        assert "RuntimeError" in content
        assert "THIS_IS_A_SYNTHETIC_SECRET" not in content

    @pytest.mark.parametrize(
        ("message", "secret"),
        [
            ("Authorization: Bearer bearer-secret-value", "bearer-secret-value"),
            ("SERVICE_API_KEY=environment-secret-value", "environment-secret-value"),
            ('{"token": "json-secret-value"}', "json-secret-value"),
            ("postgresql://user:database-secret@example.test/db", "database-secret"),
            ("https://user:userinfo-secret@example.test/path", "userinfo-secret"),
            ("https://example.test/path?token=query-secret&safe=visible", "query-secret"),
            (
                "https://discord.com/api/webhooks/123456789/discord-webhook-secret",
                "discord-webhook-secret",
            ),
            (
                "https://hooks.slack.com/services/T000/B000/slack-webhook-secret",
                "slack-webhook-secret",
            ),
            (
                "-----BEGIN PRIVATE KEY-----\nprivate-secret\n-----END PRIVATE KEY-----",
                "private-secret",
            ),
        ],
    )
    def test_persisted_messages_redact_sensitive_formats(
        self, hermes_home, message, secret
    ):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        logging.getLogger("test_hermes_logging.redaction_formats").warning(message)
        for handler in logging.getLogger().handlers:
            handler.flush()
        content = (hermes_home / "logs" / "errors.log").read_text()
        assert secret not in content

    def test_nested_exception_chain_is_redacted_after_formatting(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        test_logger = logging.getLogger("test_hermes_logging.nested_redaction")
        try:
            try:
                raise ValueError("https://example.test/?access_token=nested-secret")
            except ValueError as cause:
                raise RuntimeError("outer synthetic failure") from cause
        except RuntimeError:
            test_logger.exception("nested exception")
        for handler in logging.getLogger().handlers:
            handler.flush()
        content = (hermes_home / "logs" / "errors.log").read_text()
        assert "ValueError" in content
        assert "RuntimeError" in content
        assert "nested-secret" not in content

    def test_reads_config_yaml(self, hermes_home):
        """setup_logging reads logging.level from config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 2, "backup_count": 1}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.DEBUG
        assert agent_handlers[0].maxBytes == 2 * 1024 * 1024
        assert agent_handlers[0].backupCount == 1

    def test_explicit_params_override_config(self, hermes_home):
        """Explicit function params take precedence over config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG"}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        hermes_logging.setup_logging(hermes_home=hermes_home, log_level="WARNING")

        root = logging.getLogger()
        agent_handlers = [
            h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.WARNING

    def test_zero_backup_count_is_preserved(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, backup_count=0)
        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        assert handler.backupCount == 0

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"max_size_mb": 0}, ValueError),
            ({"max_size_mb": -1}, ValueError),
            ({"backup_count": -1}, ValueError),
            ({"max_size_mb": True}, TypeError),
            ({"backup_count": False}, TypeError),
            ({"log_level": ""}, ValueError),
            ({"log_level": "TRACE"}, ValueError),
            ({"log_level": 10}, TypeError),
        ],
    )
    def test_invalid_explicit_configuration_fails_before_mutation(
        self, hermes_home, kwargs, error
    ):
        root = logging.getLogger()
        before = list(root.handlers)
        with pytest.raises(error):
            hermes_logging.setup_logging(hermes_home=hermes_home, **kwargs)
        assert root.handlers == before

    def test_invalid_yaml_fields_warn_and_use_defaults(self, hermes_home, capsys):
        import yaml
        config = {
            "logging": {
                "level": "TRACE",
                "max_size_mb": False,
                "backup_count": -1,
            }
        }
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        assert handler.level == logging.INFO
        assert handler.maxBytes == 5 * 1024 * 1024
        assert handler.backupCount == 3
        stderr = capsys.readouterr().err
        assert "logging.level" in stderr
        assert "logging.max_size_mb" in stderr
        assert "logging.backup_count" in stderr
        assert "TRACE" not in stderr

    def test_malformed_yaml_warns_and_uses_defaults(self, hermes_home, capsys):
        (hermes_home / "config.yaml").write_text("logging: [unterminated")
        hermes_logging.setup_logging(hermes_home=hermes_home)
        assert "unable to read logging configuration" in capsys.readouterr().err
        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        assert handler.level == logging.INFO

    def test_explicit_home_reads_its_own_configuration(self, tmp_path):
        import yaml
        first = tmp_path / "profile-one"
        second = tmp_path / "profile-two"
        first.mkdir()
        second.mkdir()
        (first / "config.yaml").write_text(yaml.dump({"logging": {"level": "DEBUG"}}))
        (second / "config.yaml").write_text(yaml.dump({"logging": {"level": "ERROR"}}))

        hermes_logging.setup_logging(hermes_home=first)
        first_handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        assert first_handler.level == logging.DEBUG
        hermes_logging.setup_logging(hermes_home=second, force=True)
        second_handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_hermes_log_kind", None) == "agent"
        )
        assert second_handler.level == logging.ERROR
        assert Path(second_handler.baseFilename).parent == second / "logs"


class TestSetupVerboseLogging:
    """setup_verbose_logging() adds a DEBUG-level console handler."""

    def test_adds_stream_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_verbose_logging()

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_hermes_verbose", False)
        ]
        assert len(verbose_handlers) == 1
        assert verbose_handlers[0].level == logging.DEBUG

    def test_idempotent(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_verbose_logging()
        hermes_logging.setup_verbose_logging()  # second call

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_hermes_verbose", False)
        ]
        assert len(verbose_handlers) == 1


class TestAddRotatingHandler:
    """_add_rotating_handler() is idempotent and creates the directory."""

    def test_creates_directory(self, tmp_path):
        log_path = tmp_path / "subdir" / "test.log"
        logger = logging.getLogger("_test_rotating")
        formatter = logging.Formatter("%(message)s")

        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        assert log_path.parent.is_dir()
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_no_duplicate_for_same_path(self, tmp_path):
        log_path = tmp_path / "test.log"
        logger = logging.getLogger("_test_rotating_dup")
        formatter = logging.Formatter("%(message)s")

        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )
        hermes_logging._add_rotating_handler(
            logger, log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        rotating_handlers = [
            h for h in logger.handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert len(rotating_handlers) == 1
        # Clean up
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
                h.close()

    def test_rollover_retains_backup_and_continues_logging(self, tmp_path):
        path = tmp_path / "rotate.log"
        logger = logging.getLogger("_test_rollover")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        handler = hermes_logging._add_rotating_handler(
            logger, path, level=logging.INFO, max_bytes=80, backup_count=1,
            formatter=logging.Formatter("prefix:%(message)s"),
            managed_kind="agent",
        )
        try:
            logger.info("a" * 60)
            logger.info("b" * 60)
            logger.info("after-rollover")
            handler.flush()
            assert (tmp_path / "rotate.log.1").exists()
            combined = path.read_text() + (tmp_path / "rotate.log.1").read_text()
            assert "prefix:" in combined
            assert "after-rollover" in combined
        finally:
            logger.removeHandler(handler)
            handler.close()

    def test_zero_backups_disables_rollover(self, tmp_path):
        path = tmp_path / "no-rollover.log"
        logger = logging.getLogger("_test_no_rollover")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        handler = hermes_logging._add_rotating_handler(
            logger, path, level=logging.INFO, max_bytes=40, backup_count=0,
            formatter=logging.Formatter("%(message)s"),
            managed_kind="agent",
        )
        try:
            logger.info("x" * 100)
            logger.info("still-active")
            handler.flush()
            assert path.stat().st_size > 40
            assert not list(tmp_path.glob("no-rollover.log.*"))
            assert "still-active" in path.read_text()
        finally:
            logger.removeHandler(handler)
            handler.close()


class TestReadLoggingConfig:
    """_read_logging_config() reads from config.yaml."""

    def test_returns_none_when_no_config(self, hermes_home):
        level, max_size, backup = hermes_logging._read_logging_config()
        assert level is None
        assert max_size is None
        assert backup is None

    def test_reads_logging_section(self, hermes_home):
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 10, "backup_count": 5}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup = hermes_logging._read_logging_config()
        assert level == "DEBUG"
        assert max_size == 10
        assert backup == 5

    def test_handles_missing_logging_section(self, hermes_home):
        import yaml
        config = {"model": "test"}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup = hermes_logging._read_logging_config()
        assert level is None
