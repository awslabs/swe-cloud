"""Logging in gocraft/work's format: `ERROR: <key> - <error>` on stderr."""

import sys
import threading
import time

_lock = threading.Lock()


def _write(level: str, key: str, message: object) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {level}: {key} - {message}\n"
    with _lock:
        sys.stderr.write(line)
        sys.stderr.flush()


def log_error(key: str, err: object) -> None:
    _write("ERROR", key, err)


def log_info(key: str, message: object) -> None:
    _write("INFO", key, message)
