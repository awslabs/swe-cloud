"""Orchestrator HTTP API.

    GET  /health                          200 once Postgres and Valkey answer
    GET  /flow_runs/<id>                  the run and its current state
    POST /flow_runs/<id>/set_state        {type, name?, lease_id?, scheduled_time?, message?, force?}
                                          -> {status: ACCEPT|REJECT|ABORT, reason, state, run}
    POST /leases/<id>/renew               {lease_duration} -> 204, or 410 if the lease is gone

Run with `python -m orchestrator.server` (PG_DSN, VALKEY_URL, ORCH_PORT, LEASE_TTL_S, SLOT_WAIT_S).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

from . import config, db
from .release import is_released
from .leases import LeaseStorage
from .policy import CORE_POLICY, MINIMAL_POLICY, OrchestrationContext, orchestrate
from .states import RUNNING, State

logger = logging.getLogger("orchestrator.server")

SETTINGS = config.load()
LEASES: LeaseStorage | None = None
_local = threading.local()


def _conn() -> psycopg.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed or conn.broken:
        conn = db.connect(SETTINGS.pg_dsn)
        _local.conn = conn
    return conn


def _initial_state(run: dict[str, Any]) -> State:
    return State(
        type=run["state_type"],
        name=run["state_name"],
        lease_id=run["lease_id"],
        scheduled_time=run["scheduled_time"],
    )


def _write_state(ctx: OrchestrationContext) -> None:
    state = ctx.proposed_state
    run = ctx.run
    run_count = run["run_count"] + (1 if state.type == RUNNING else 0)
    updates = {"run_count": run_count, **ctx.run_updates}
    row = ctx.conn.execute(
        """
        INSERT INTO flow_run_state (flow_run_id, type, name, lease_id, scheduled_time, message)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING ts
        """,
        (run["id"], state.type, state.name, state.lease_id, state.scheduled_time, state.message),
    ).fetchone()
    assignments = ", ".join(f"{column} = %({column})s" for column in updates)
    ctx.conn.execute(
        f"""
        UPDATE flow_run SET state_type = %(state_type)s, state_name = %(state_name)s,
               lease_id = %(lease_id)s, scheduled_time = %(scheduled_time)s, state_ts = %(ts)s,
               {assignments}
         WHERE id = %(id)s
        """,
        {
            "state_type": state.type,
            "state_name": state.name,
            "lease_id": state.lease_id,
            "scheduled_time": state.scheduled_time,
            "ts": row["ts"],
            "id": run["id"],
            **updates,
        },
    )
    run.update(updates)


def set_state(run_id: uuid.UUID, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    proposed = State.from_request(body)
    force = bool(body.get("force"))
    conn = _conn()
    with conn.transaction():
        run = conn.execute("SELECT * FROM flow_run WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if run is None:
            return 404, {"detail": "flow run not found"}
        ctx = OrchestrationContext(conn, LEASES, SETTINGS, run, _initial_state(run), proposed)
        orchestrate(ctx, MINIMAL_POLICY if force else CORE_POLICY, _write_state)
    for action in ctx.after_commit:
        action()
    validated = ctx.proposed_state
    return 200, {
        "status": ctx.response_status,
        "reason": ctx.reason,
        "state": validated.to_json() if validated else _initial_state(run).to_json(),
        "run": {"id": str(run["id"]), "run_count": run["run_count"], "retry_type": run.get("retry_type")},
    }


def renew_lease(lease_id: uuid.UUID, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    duration = float(body.get("lease_duration", 300))
    if duration <= 0:
        return 422, {"detail": "lease_duration must be positive"}
    conn = _conn()
    if is_released(conn, lease_id):
        LEASES.revoke_lease(lease_id)  # a record left behind by a release that died mid-way
        return 410, {"detail": "Lease not found - it may have expired or been revoked"}
    if not LEASES.renew_lease(lease_id, ttl=timedelta(seconds=duration)):
        return 410, {"detail": "Lease not found - it may have expired or been revoked"}
    return 204, None


def read_run(run_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
    conn = _conn()
    with conn.transaction():
        run = conn.execute("SELECT * FROM flow_run WHERE id = %s", (run_id,)).fetchone()
    if run is None:
        return 404, {"detail": "flow run not found"}
    return 200, {
        "id": str(run["id"]),
        "deployment_id": str(run["deployment_id"]) if run["deployment_id"] else None,
        "run_count": run["run_count"],
        "retry_type": run["retry_type"],
        "parameters": run["parameters"],
        "state": _initial_state(run).to_json(),
    }


_SET_STATE = re.compile(r"^/flow_runs/([0-9a-fA-F-]{36})/set_state$")
_RUN = re.compile(r"^/flow_runs/([0-9a-fA-F-]{36})$")
_RENEW = re.compile(r"^/leases/([0-9a-fA-F-]{36})/renew$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _reply(self, status: int, payload: dict[str, Any] | None) -> None:
        data = b"" if payload is None else json.dumps(payload, default=str).encode()
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                _conn().execute("SELECT 1")
                LEASES.ping()
                self._reply(200, {"ok": True})
            elif m := _RUN.match(self.path):
                self._reply(*read_run(uuid.UUID(m.group(1))))
            else:
                self._reply(404, {"detail": "not found"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET %s failed", self.path)
            self._reply(500, {"detail": str(exc)})

    def do_POST(self) -> None:
        try:
            body = self._body()
            if m := _SET_STATE.match(self.path):
                self._reply(*set_state(uuid.UUID(m.group(1)), body))
            elif m := _RENEW.match(self.path):
                self._reply(*renew_lease(uuid.UUID(m.group(1)), body))
            else:
                self._reply(404, {"detail": "not found"})
        except ValueError as exc:
            self._reply(422, {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST %s failed", self.path)
            self._reply(500, {"detail": str(exc)})


def main() -> None:
    global LEASES
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not SETTINGS.pg_dsn:
        raise SystemExit("PG_DSN is not set (see README: python -m orchestrator.devstack up)")
    db.ensure_schema(db.connect(SETTINGS.pg_dsn))
    LEASES = LeaseStorage(SETTINGS.valkey_url)
    server = ThreadingHTTPServer(("127.0.0.1", SETTINGS.port), Handler)
    server.daemon_threads = True
    logger.info("orchestrator listening on 127.0.0.1:%s", SETTINGS.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
