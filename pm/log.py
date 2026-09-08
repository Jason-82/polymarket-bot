"""Structured-ish logging on the standard library: `event key=value ...` lines."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class KV(logging.LoggerAdapter):
    """logger.info("event", key=value, ...) -> 'event key=value ...'"""

    def log(self, level: int, msg: object, *args: Any, **kwargs: Any) -> None:
        if not self.isEnabledFor(level):
            return
        exc_info = kwargs.pop("exc_info", None)
        stack_info = kwargs.pop("stack_info", False)
        extra = kwargs.pop("extra", None)
        fields = " ".join(f"{k}={_fmt(v)}" for k, v in kwargs.items())
        text = f"{msg} {fields}".rstrip()
        self.logger.log(level, text, *args, exc_info=exc_info, stack_info=stack_info, extra=extra)

    def process(self, msg, kwargs):  # pragma: no cover - not used with our log()
        return msg, kwargs


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    s = str(v)
    return f'"{s}"' if " " in s else s


def get_logger(name: str) -> KV:
    return KV(logging.getLogger(name), {})


def setup(level: str = "INFO", log_dir: str | Path = "data") -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(Path(log_dir) / "pm.log", maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root.addHandler(fh)

    # Third-party chatter
    for noisy in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
