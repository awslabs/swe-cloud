"""Settings of the scale-up function, read from its environment (see deploy/scale-up.json)."""

from __future__ import annotations

import json
import os


def yn(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "y", "yes", "true", "on")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def fleet_api_url() -> str:
    return env("FLEET_API_URL", "http://127.0.0.1:8081").rstrip("/")


def github_api_url() -> str:
    return env("GITHUB_API_URL", "http://127.0.0.1:8082").rstrip("/")


def scale_errors() -> list[str]:
    return json.loads(env("SCALE_ERRORS", "[]"))
