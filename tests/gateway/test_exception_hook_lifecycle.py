"""Gateway integration tests for background exception hooks."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from gateway import run as gateway_run


@pytest.mark.asyncio
async def test_gateway_installs_and_restores_exception_hooks(tmp_path):
    runner = AsyncMock()
    runner.start.return_value = True
    runner.should_exit_cleanly = True
    runner.exit_reason = None

    with patch("gateway.status.get_running_pid", return_value=None), \
         patch("tools.skills_sync.sync_skills"), \
         patch.object(gateway_run, "_hermes_home", tmp_path), \
         patch.object(gateway_run, "GatewayRunner", return_value=runner), \
         patch("hermes_exception_hooks.install_thread_exception_hook", return_value=True) as install_thread, \
         patch("hermes_exception_hooks.install_asyncio_exception_handler", return_value=True) as install_asyncio, \
         patch("hermes_exception_hooks.restore_thread_exception_hook") as restore_thread, \
         patch("hermes_exception_hooks.restore_asyncio_exception_handler") as restore_asyncio:
        assert await gateway_run.start_gateway(verbosity=None) is True

    install_thread.assert_called_once_with()
    install_asyncio.assert_called_once()
    restore_asyncio.assert_called_once_with(install_asyncio.call_args.args[0])
    restore_thread.assert_called_once_with()


def test_gateway_cron_ticker_logs_escaping_exception(caplog):
    class OneTickStop:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.stopped = True

    with patch("cron.scheduler.tick", side_effect=RuntimeError("ticker exploded")), \
         patch("gateway.platforms.base.cleanup_image_cache"), \
         patch("gateway.platforms.base.cleanup_document_cache"):
        with caplog.at_level(logging.ERROR, logger="gateway.run"):
            gateway_run._start_cron_ticker(OneTickStop(), interval=1)

    record = next(r for r in caplog.records if "Gateway cron tick failed" in r.message)
    assert record.exc_info[0] is RuntimeError
