"""Routing tests for the gateway-specific log handler."""

import logging

from agent.redact import RedactingFormatter
from hermes_logging import _LOG_FORMAT, _add_rotating_handler
from hermes_logging_context import (
    GatewayLogFilter,
    HermesLogContextFilter,
    add_filter_once,
)


def test_gateway_filter_routing_matrix(tmp_path):
    logger = logging.getLogger("test.gateway.routing.root")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    path = tmp_path / "gateway.log"
    handler = _add_rotating_handler(
        logger,
        path,
        level=logging.INFO,
        max_bytes=1024 * 1024,
        backup_count=1,
        formatter=RedactingFormatter(_LOG_FORMAT),
    )
    add_filter_once(handler, HermesLogContextFilter)
    add_filter_once(handler, GatewayLogFilter)

    records = [
        logging.LogRecord("gateway.run", logging.INFO, __file__, 1, "gateway-owned", (), None),
        logging.LogRecord("agent.worker", logging.WARNING, __file__, 1, "agent-owned", (), None),
        logging.LogRecord("cron.scheduler", logging.ERROR, __file__, 1, "cron-owned", (), None),
        logging.LogRecord("third.party", logging.ERROR, __file__, 1, "third-party", (), None),
    ]
    marked = logging.LogRecord(
        "hermes_exception_hooks", logging.ERROR, __file__, 1,
        "marked Authorization: Bearer gateway-secret-value", (), None,
    )
    marked.hermes_component = "gateway"
    records.append(marked)
    for record in records:
        logger.handle(record)
    handler.flush()

    content = path.read_text()
    assert "gateway-owned" in content
    assert "marked" in content
    assert "gateway-secret-value" not in content
    assert "agent-owned" not in content
    assert "cron-owned" not in content
    assert "third-party" not in content
    handler.close()
    logger.handlers.clear()


def test_handler_and_filters_are_idempotent(tmp_path):
    logger = logging.getLogger("test.gateway.routing.idempotent")
    logger.propagate = False
    path = tmp_path / "gateway.log"
    formatter = RedactingFormatter(_LOG_FORMAT)
    first = _add_rotating_handler(
        logger, path, level=logging.INFO, max_bytes=1024,
        backup_count=1, formatter=formatter,
    )
    second = _add_rotating_handler(
        logger, path, level=logging.INFO, max_bytes=1024,
        backup_count=1, formatter=formatter,
    )
    assert first is second
    add_filter_once(first, HermesLogContextFilter)
    add_filter_once(first, HermesLogContextFilter)
    add_filter_once(first, GatewayLogFilter)
    add_filter_once(first, GatewayLogFilter)
    assert sum(isinstance(item, HermesLogContextFilter) for item in first.filters) == 1
    assert sum(isinstance(item, GatewayLogFilter) for item in first.filters) == 1
    first.close()
    logger.handlers.clear()
