"""Execution-scoped context for correlated Hermes log records.

Importing this module has no side effects.  Cron owns context installation at
the per-run boundary; logging handlers only read the current values.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_cron_job_id: ContextVar[str] = ContextVar("hermes_cron_job_id", default="-")
_cron_run_id: ContextVar[str] = ContextVar("hermes_cron_run_id", default="-")
_cron_profile: ContextVar[str] = ContextVar("hermes_cron_profile", default="-")


def get_log_context() -> tuple[str, str, str]:
    """Return the active ``(job_id, run_id, profile)`` logging context."""
    return _cron_job_id.get(), _cron_run_id.get(), _cron_profile.get()


@contextmanager
def cron_log_context(*, job_id: str, run_id: str, profile: str) -> Iterator[None]:
    """Bind correlation fields for one cron execution and always reset them."""
    job_token = _cron_job_id.set(str(job_id or "-"))
    run_token = _cron_run_id.set(str(run_id or "-"))
    profile_token = _cron_profile.set(str(profile or "-"))
    try:
        yield
    finally:
        _cron_profile.reset(profile_token)
        _cron_run_id.reset(run_token)
        _cron_job_id.reset(job_token)


class HermesLogContextFilter(logging.Filter):
    """Inject safe correlation defaults into every record reaching a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        job_id, run_id, profile = get_log_context()
        record.job_id = job_id
        record.run_id = run_id
        record.profile = profile
        if job_id == run_id == profile == "-":
            record.hermes_context = ""
        else:
            record.hermes_context = (
                f"[job_id={job_id} run_id={run_id} profile={profile}] "
            )
        return True


class GatewayLogFilter(logging.Filter):
    """Keep only explicitly gateway-owned records in ``gateway.log``."""

    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.name == "gateway"
            or record.name.startswith("gateway.")
            or getattr(record, "hermes_component", None) == "gateway"
        )


def add_filter_once(handler: logging.Handler, filter_type: type[logging.Filter]) -> None:
    """Attach one instance of *filter_type* to *handler*."""
    if not any(isinstance(existing, filter_type) for existing in handler.filters):
        handler.addFilter(filter_type())
