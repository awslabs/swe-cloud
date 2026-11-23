"""Structured logging in the shape Powertools emits: one JSON object per line, on stderr.

stdout belongs to the Lambda runtime adapter (it carries the function's response), so nothing in the
control plane may print there.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

_context: dict[str, Any] = {}


def add_persistent_context(**keys: Any) -> None:
    _context.update(keys)


class Logger:
    def __init__(self, service: str, **keys: Any) -> None:
        self.service = service
        self.keys = keys

    def child(self, **keys: Any) -> "Logger":
        return Logger(self.service, **{**self.keys, **keys})

    def _emit(self, level: str, message: str, extra: dict[str, Any]) -> None:
        record = {
            "level": level,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
            "service": self.service,
            "message": message,
            **_context,
            **self.keys,
            **extra,
        }
        sys.stderr.write(json.dumps(record, default=str) + "\n")
        sys.stderr.flush()

    def debug(self, message: str, **extra: Any) -> None:
        self._emit("DEBUG", message, extra)

    def info(self, message: str, **extra: Any) -> None:
        self._emit("INFO", message, extra)

    def warn(self, message: str, **extra: Any) -> None:
        self._emit("WARN", message, extra)

    def error(self, message: str, **extra: Any) -> None:
        self._emit("ERROR", message, extra)


def create_child_logger(service: str) -> Logger:
    return Logger(service)
