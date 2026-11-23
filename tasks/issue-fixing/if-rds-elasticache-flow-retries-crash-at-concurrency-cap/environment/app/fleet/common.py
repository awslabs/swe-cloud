"""Shared plumbing for fleet processes: orchestrator client, event log, settings."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

ORCH_URL = os.environ.get("ORCH_URL", "http://127.0.0.1:4200").rstrip("/")
PG_DSN = os.environ.get("PG_DSN", "")
# Lease duration the engine asks for on every renewal; it renews at 0.75 of it.
LEASE_DURATION_S = float(os.environ.get("LEASE_DURATION_S", "300"))
RENEWAL_FRACTION = 0.75
WORKER_POLL_S = float(os.environ.get("WORKER_POLL_S", "5"))
_EVENTS = os.environ.get("FLEET_EVENTS")  # optional JSON-lines event log
_lock = threading.Lock()


def log(role: str, message: str, **event: Any) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    sys.stderr.write(f"{stamp} {role}[{os.getpid()}] {message}\n")
    sys.stderr.flush()
    if _EVENTS and event:
        line = json.dumps({"t": time.time(), "role": role, "pid": os.getpid(), **event}, default=str)
        with _lock, open(_EVENTS, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class HTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


def request(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ORCH_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        if exc.code >= 500:
            raise HTTPError(exc.code, raw) from None
        return exc.code, (json.loads(raw) if raw.startswith("{") else raw)


def set_state(run_id: str, state_type: str, name: str | None = None, *, message: str | None = None,
              scheduled_time: str | None = None, force: bool = False, attempts: int = 5) -> dict[str, Any]:
    body = {"type": state_type, "name": name, "lease_id": None, "message": message,
            "scheduled_time": scheduled_time, "force": force}
    delay = 0.5
    for attempt in range(attempts):
        try:
            status, payload = request("POST", f"/flow_runs/{run_id}/set_state", body)
            if status != 200:
                raise HTTPError(status, json.dumps(payload))
            return payload
        except (OSError, HTTPError) as exc:
            if attempt == attempts - 1:
                raise
            log("client", f"set_state {state_type} for {run_id} failed ({exc}); retrying in {delay:.1f}s")
            time.sleep(delay)
            delay = min(delay * 2, 5)
    raise AssertionError("unreachable")


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
