"""Centralized logging setup for Hermes Agent.

Provides a single ``setup_logging()`` entry point that both the CLI and
gateway call early in their startup path.  All log files live under
``~/.hermes/logs/`` (profile-aware via ``get_hermes_home()``).

Log files produced:
    agent.log   — INFO+, all agent/tool/session activity (the main log)
    errors.log  — WARNING+, errors and warnings only (quick triage)

Both files use ``RotatingFileHandler`` with ``RedactingFormatter`` so
secrets are never written to disk.
"""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

# Sentinel to track whether setup_logging() has already run.  The function
# is idempotent — calling it twice is safe but the second call is a no-op
# unless ``force=True``.
_logging_initialized = False
_configuration_lock = threading.RLock()

_DEFAULT_LEVEL = "INFO"
_DEFAULT_MAX_SIZE_MB = 5
_DEFAULT_BACKUP_COUNT = 3
_ERROR_MAX_BYTES = 2 * 1024 * 1024
_ERROR_BACKUP_COUNT = 2
_SUPPORTED_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "FATAL": logging.CRITICAL,
    "CRITICAL": logging.CRITICAL,
}

# Default log format — includes timestamp, level, logger name, and message.
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(hermes_context)s%(message)s"
_LOG_FORMAT_VERBOSE = "%(asctime)s - %(name)s - %(levelname)s - %(hermes_context)s%(message)s"

# Third-party loggers that are noisy at DEBUG/INFO level.
_NOISY_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
    "asyncio",
    "hpack",
    "hpack.hpack",
    "grpc",
    "modal",
    "urllib3",
    "urllib3.connectionpool",
    "websockets",
    "charset_normalizer",
    "markdown_it",
)


def setup_logging(
    *,
    hermes_home: Optional[Path] = None,
    log_level: Optional[str] = None,
    max_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Configure the Hermes logging subsystem.

    Safe to call multiple times — the second call is a no-op unless
    *force* is ``True``.

    Parameters
    ----------
    hermes_home
        Override for the Hermes home directory.  Falls back to
        ``get_hermes_home()`` (profile-aware).
    log_level
        Minimum level for the ``agent.log`` file handler.  Accepts any
        standard Python level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        Defaults to ``"INFO"`` or the value from config.yaml ``logging.level``.
    max_size_mb
        Maximum size of each log file in megabytes before rotation.
        Defaults to 5 or the value from config.yaml ``logging.max_size_mb``.
    backup_count
        Number of rotated backup files to keep.
        Defaults to 3 or the value from config.yaml ``logging.backup_count``.
    mode
        Hint for the caller context: ``"cli"``, ``"gateway"``, ``"cron"``.
        Reserved for caller-specific setup compatibility.
    force
        Re-run setup even if it has already been called.

    Returns
    -------
    Path
        The ``logs/`` directory where files are written.
    """
    global _logging_initialized
    with _configuration_lock:
        home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
        requested_log_dir = home / "logs"

        cfg_level, cfg_max_size, cfg_backup = _read_logging_config(home)
        level = _resolve_level(log_level, cfg_level)
        max_size = _resolve_integer(
            "max_size_mb", max_size_mb, cfg_max_size,
            default=_DEFAULT_MAX_SIZE_MB, minimum=1,
        )
        backups = _resolve_integer(
            "backup_count", backup_count, cfg_backup,
            default=_DEFAULT_BACKUP_COUNT, minimum=0,
        )

        from agent.redact import RedactingFormatter
        from hermes_logging_context import HermesLogContextFilter, add_filter_once

        root = logging.getLogger()
        desired = {
            "agent": {
                "path": requested_log_dir / "agent.log",
                "level": level,
                "max_bytes": max_size * 1024 * 1024,
                "backup_count": backups,
            },
            "errors": {
                "path": requested_log_dir / "errors.log",
                "level": logging.WARNING,
                "max_bytes": _ERROR_MAX_BYTES,
                "backup_count": _ERROR_BACKUP_COUNT,
            },
        }
        managed = {kind: _find_managed_handler(root, kind) for kind in desired}

        # Adopt only the narrow historical shape Hermes created before handler
        # ownership metadata existed. An arbitrary same-path file handler is
        # left untouched.
        for kind, spec in desired.items():
            if managed[kind] is None:
                managed[kind] = _find_legacy_handler(root, kind=kind, **spec)

        if _logging_initialized and not force and all(managed.values()):
            existing_dir = Path(managed["agent"].baseFilename).parent
            return existing_dir

        # A non-forced repair follows the surviving managed handler's profile.
        # A forced call always targets the explicitly resolved profile.
        if not force:
            survivor = managed["agent"] or managed["errors"]
            if survivor is not None:
                repair_dir = Path(survivor.baseFilename).parent
                desired["agent"]["path"] = repair_dir / "agent.log"
                desired["errors"]["path"] = repair_dir / "errors.log"

        replacements = {}
        updates = {}
        try:
            for kind, spec in desired.items():
                existing = managed[kind]
                same_path = existing is not None and _handler_path(existing) == spec["path"].resolve()
                if same_path:
                    if force or not _logging_initialized:
                        updates[kind] = (existing, spec)
                    continue
                replacements[kind] = _create_rotating_handler(
                    formatter=RedactingFormatter(_LOG_FORMAT),
                    managed_kind=kind,
                    **spec,
                )
        except Exception:
            for handler in replacements.values():
                handler.close()
            raise

        # All replacement files opened successfully. Attach every replacement
        # before changing existing handlers. If attachment fails (including a
        # custom Logger implementation that attaches and then raises), remove
        # and close every prepared replacement, leaving the old configuration
        # untouched.
        try:
            for handler in replacements.values():
                add_filter_once(handler, HermesLogContextFilter)
                root.addHandler(handler)
        except Exception:
            for handler in replacements.values():
                if handler in root.handlers:
                    root.removeHandler(handler)
                handler.close()
            raise

        # Attachment is complete. Same-path updates cannot affect the profile
        # swap and preserve the existing handler's filter instances.
        for kind, (handler, spec) in updates.items():
            _configure_rotating_handler(
                handler,
                level=spec["level"],
                max_bytes=spec["max_bytes"],
                backup_count=spec["backup_count"],
                formatter=RedactingFormatter(_LOG_FORMAT),
                managed_kind=kind,
            )
        for kind in replacements:
            previous = managed[kind]
            if previous is not None:
                root.removeHandler(previous)
                previous.flush()
                previous.close()

        final_handlers = {
            kind: replacements.get(kind) or updates.get(kind, (managed[kind],))[0]
            for kind in desired
        }
        for handler in final_handlers.values():
            add_filter_once(handler, HermesLogContextFilter)

        if root.level != logging.NOTSET and root.level > level:
            root.setLevel(level)
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

        _logging_initialized = True
        return Path(final_handlers["agent"].baseFilename).parent


def setup_verbose_logging() -> None:
    """Enable DEBUG-level console logging for ``--verbose`` / ``-v`` mode.

    Called by ``AIAgent.__init__()`` when ``verbose_logging=True``.
    """
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # Avoid adding duplicate stream handlers.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            if getattr(h, "_hermes_verbose", False):
                return

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt="%H:%M:%S"))
    from hermes_logging_context import HermesLogContextFilter, add_filter_once
    add_filter_once(handler, HermesLogContextFilter)
    handler._hermes_verbose = True  # type: ignore[attr-defined]
    handler._hermes_managed = True  # type: ignore[attr-defined]
    handler._hermes_log_kind = "verbose-console"  # type: ignore[attr-defined]
    root.addHandler(handler)

    # Lower root logger level so DEBUG records reach all handlers.
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    # Keep third-party libraries at WARNING to reduce noise.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # rex-deploy at INFO for sandbox status.
    logging.getLogger("rex-deploy").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_rotating_handler(
    logger: logging.Logger,
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    managed_kind: Optional[str] = None,
    reconfigure: bool = False,
) -> RotatingFileHandler:
    """Add a ``RotatingFileHandler`` to *logger*, skipping if one already
    exists for the same resolved file path (idempotent).
    """
    with _configuration_lock:
        resolved = path.resolve()
        for existing in logger.handlers:
            if not isinstance(existing, RotatingFileHandler):
                continue
            same_kind = managed_kind and (
                getattr(existing, "_hermes_managed", False)
                and getattr(existing, "_hermes_log_kind", None) == managed_kind
            )
            same_path = _handler_path(existing) == resolved
            legacy_gateway = (
                managed_kind == "gateway"
                and same_path
                and any(type(item).__name__ == "GatewayLogFilter" for item in existing.filters)
            )
            if same_path and managed_kind is None:
                return existing
            if same_kind or legacy_gateway:
                if reconfigure and same_kind and not same_path:
                    replacement = _create_rotating_handler(
                        path=path, level=level, max_bytes=max_bytes,
                        backup_count=backup_count, formatter=formatter,
                        managed_kind=managed_kind,
                    )
                    for item in existing.filters:
                        replacement.addFilter(item)
                    try:
                        logger.addHandler(replacement)
                    except Exception:
                        if replacement in logger.handlers:
                            logger.removeHandler(replacement)
                        replacement.close()
                        raise
                    logger.removeHandler(existing)
                    existing.flush()
                    existing.close()
                    return replacement
                if reconfigure:
                    _configure_rotating_handler(
                        existing, level=level, max_bytes=max_bytes,
                        backup_count=backup_count, formatter=formatter,
                        managed_kind=managed_kind,
                    )
                return existing

        handler = _create_rotating_handler(
            path=path, level=level, max_bytes=max_bytes,
            backup_count=backup_count, formatter=formatter,
            managed_kind=managed_kind,
        )
        try:
            logger.addHandler(handler)
        except Exception:
            if handler in logger.handlers:
                logger.removeHandler(handler)
            handler.close()
            raise
        return handler


def _handler_path(handler: RotatingFileHandler) -> Path:
    return Path(handler.baseFilename).resolve()


def _create_rotating_handler(
    *, path: Path, level: int, max_bytes: int, backup_count: int,
    formatter: logging.Formatter, managed_kind: Optional[str] = None,
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
    )
    _configure_rotating_handler(
        handler, level=level, max_bytes=max_bytes,
        backup_count=backup_count, formatter=formatter,
        managed_kind=managed_kind,
    )
    return handler


def _configure_rotating_handler(
    handler: RotatingFileHandler, *, level: int, max_bytes: int,
    backup_count: int, formatter: logging.Formatter,
    managed_kind: Optional[str],
) -> None:
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.maxBytes = max_bytes
    handler.backupCount = backup_count
    if managed_kind:
        handler._hermes_managed = True  # type: ignore[attr-defined]
        handler._hermes_log_kind = managed_kind  # type: ignore[attr-defined]


def _find_managed_handler(
    logger: logging.Logger, kind: str,
) -> Optional[RotatingFileHandler]:
    for handler in logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and getattr(handler, "_hermes_managed", False)
            and getattr(handler, "_hermes_log_kind", None) == kind
        ):
            return handler
    return None


def _find_legacy_handler(
    logger: logging.Logger, *, kind: str, path: Path, level: int,
    max_bytes: int, backup_count: int,
) -> Optional[RotatingFileHandler]:
    for handler in logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and not getattr(handler, "_hermes_managed", False)
            and _handler_path(handler) == path.resolve()
            and handler.maxBytes == max_bytes
            and handler.backupCount == backup_count
        ):
            handler._hermes_managed = True  # type: ignore[attr-defined]
            handler._hermes_log_kind = kind  # type: ignore[attr-defined]
            return handler
    return None


def _resolve_level(explicit, configured) -> int:
    value = explicit if explicit is not None else configured
    if value is None:
        value = _DEFAULT_LEVEL
    if not isinstance(value, str):
        if explicit is not None:
            raise TypeError("log_level must be a string")
        _warn_invalid_config("level")
        value = _DEFAULT_LEVEL
    normalized = value.strip().upper()
    if normalized not in _SUPPORTED_LEVELS:
        if explicit is not None:
            raise ValueError("unsupported log_level")
        _warn_invalid_config("level")
        normalized = _DEFAULT_LEVEL
    return _SUPPORTED_LEVELS[normalized]


def _resolve_integer(name, explicit, configured, *, default: int, minimum: int) -> int:
    value = explicit if explicit is not None else configured
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if explicit is not None:
            raise TypeError(f"{name} must be an integer")
        _warn_invalid_config(name)
        return default
    if value < minimum:
        if explicit is not None:
            raise ValueError(f"{name} must be at least {minimum}")
        _warn_invalid_config(name)
        return default
    return value


def _warn_invalid_config(name: str) -> None:
    print(f"Warning: invalid logging.{name}; using default", file=sys.stderr)


def _read_logging_config(hermes_home: Optional[Path] = None):
    """Best-effort read of ``logging.*`` from config.yaml.

    Returns ``(level, max_size_mb, backup_count)`` — any may be ``None``.
    """
    try:
        import yaml
        config_path = (hermes_home or get_hermes_home()) / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            log_cfg = cfg.get("logging", {})
            if isinstance(log_cfg, dict):
                return (
                    log_cfg.get("level"),
                    log_cfg.get("max_size_mb"),
                    log_cfg.get("backup_count"),
                )
    except Exception:
        print("Warning: unable to read logging configuration; using defaults", file=sys.stderr)
    return (None, None, None)
