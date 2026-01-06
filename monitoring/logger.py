"""Structured logging setup using structlog."""

import logging
import sys
from datetime import datetime
from typing import Any, Dict, Optional

# Try to use structlog, fall back to standard logging
try:
    import structlog
    HAS_STRUCTLOG = True
except ImportError:
    HAS_STRUCTLOG = False


_configured = False


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    json_format: bool = True,
) -> None:
    """
    Configure structured logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for log output
        json_format: Whether to use JSON format (default True)
    """
    global _configured

    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    if HAS_STRUCTLOG:
        _setup_structlog(log_level, log_file, json_format)
    else:
        _setup_stdlib_logging(log_level, log_file)

    _configured = True


def _setup_structlog(
    level: int,
    log_file: Optional[str],
    json_format: bool,
) -> None:
    """Configure structlog."""
    # Configure standard logging (structlog uses it as backend)
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=handlers,
    )

    # Configure structlog processors
    processors = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _setup_stdlib_logging(level: int, log_file: Optional[str]) -> None:
    """Configure standard library logging as fallback."""
    handlers = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=level,
        handlers=handlers,
    )


def get_logger(name: str) -> Any:
    """
    Get a logger instance.

    Returns a structlog logger if available, otherwise standard logging.
    """
    if HAS_STRUCTLOG:
        return structlog.get_logger(name)
    else:
        return StandardLoggerWrapper(logging.getLogger(name))


class StandardLoggerWrapper:
    """Wrapper to give stdlib logger a structlog-like interface."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _format_kwargs(self, kwargs: Dict[str, Any]) -> str:
        if not kwargs:
            return ""
        return " " + " ".join(f"{k}={v}" for k, v in kwargs.items())

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(msg + self._format_kwargs(kwargs))

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(msg + self._format_kwargs(kwargs))

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(msg + self._format_kwargs(kwargs))

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(msg + self._format_kwargs(kwargs))

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._logger.critical(msg + self._format_kwargs(kwargs))

    def exception(self, msg: str, **kwargs: Any) -> None:
        self._logger.exception(msg + self._format_kwargs(kwargs))

    def bind(self, **kwargs: Any) -> "StandardLoggerWrapper":
        """Return self (structlog compatibility)."""
        return self
