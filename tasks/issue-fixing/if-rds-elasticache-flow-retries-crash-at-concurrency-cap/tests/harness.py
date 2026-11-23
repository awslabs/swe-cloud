"""Drive the orchestrator in /app through one mixed workload with faults, and record what happened.

Nothing here imports code from /app. The server processes under test are started from /app as
`python -m orchestrator.server` and `python -m orchestrator.repossessor`; the client side (worker,
supervisor, engine) runs from the verifier's own pristine copy of the fleet. Expectations are
derived from Postgres, Valkey and the fleet's event log.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import boto3
import psycopg
import redis
from botocore.config import Config
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
LOGS = Path(os.environ.get("VERIFIER_LOGS", "/logs/verifier"))
PORT = 4200
ORCH_URL = f"http://127.0.0.1:{PORT}"

LEASE_TTL_S = 8.0
SLOT_WAIT_S = 1.0
REPOSSESS_LOOP_S = 1.0
WORKER_POLL_S = 1.0
RETRY_DELAY_S = 8
SIGTERM_AFTER_S = 1.0      # after R4's second attempt is RUNNING
SIGKILL_AFTER_S = 3.0      # after R6 / R9 are RUNNING
RESTART_AFTER_S = 3.0      # outage of a server process killed at its pause point
LATE_CRASH_AFTER_S = 6.0   # forced Crashed for R9, after the reaper was killed
RELEASE_PROBE_S = 2.0      # renew R4's lease this long after its reschedule was recorded
D4_DELAY_S = 12.0          # D4's runs become due this long after t0
STARTUP_TIMEOUT_S = 40.0
CASE_TIMEOUT_S = 160.0

LEASE_PREFIX = "prefect:concurrency:lease:"
EXPIRATIONS = "prefect:concurrency:expirations"
TERMINAL = {"COMPLETED", "FAILED", "CRASHED", "CANCELLED"}

BASE_DDL = """
CREATE TABLE IF NOT EXISTS concurrency_limit_v2 (
    id uuid PRIMARY KEY, name text NOT NULL, "limit" integer NOT NULL,
    active_slots integer NOT NULL DEFAULT 0, active boolean NOT NULL DEFAULT true,
    updated timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS deployment (
    id uuid PRIMARY KEY, name text NOT NULL UNIQUE, concurrency_limit_id uuid REFERENCES concurrency_limit_v2 (id),
    collision_strategy text NOT NULL DEFAULT 'ENQUEUE');
CREATE TABLE IF NOT EXISTS flow_run (
    id uuid PRIMARY KEY, deployment_id uuid REFERENCES deployment (id), created_seq bigserial, name text,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb, retries integer NOT NULL DEFAULT 0,
    retry_delay_s integer NOT NULL DEFAULT 0, run_count integer NOT NULL DEFAULT 0, retry_type text,
    state_type text NOT NULL, state_name text NOT NULL, lease_id uuid, scheduled_time timestamptz,
    state_ts timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS flow_run_state (
    id bigserial PRIMARY KEY, flow_run_id uuid NOT NULL REFERENCES flow_run (id), type text NOT NULL,
    name text NOT NULL, lease_id uuid, scheduled_time timestamptz, message text,
    ts timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS attempt_log (
    id bigserial PRIMARY KEY, flow_run_id uuid NOT NULL, attempt integer NOT NULL, lease_id uuid,
    body_start timestamptz NOT NULL DEFAULT clock_timestamp(), body_end timestamptz, outcome text);
"""


class EnvironmentDown(Exception):
    """The emulator or an engine never became usable: not a verdict on the submission."""


def _log(message: str) -> None:
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} [harness] {message}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------------------------
# infrastructure

@dataclass
class Stack:
    name: str
    pg_dsn: str
    valkey_url: str


def provision() -> Stack:
    cfg = Config(read_timeout=600, retries={"max_attempts": 1})
    rds = boto3.client("rds", config=cfg)
    cache = boto3.client("elasticache", config=cfg)
    name = f"verify-{uuid.uuid4().hex[:10]}"
    password = uuid.uuid4().hex
    errors: list[BaseException] = []

    def make_db():
        try:
            rds.create_db_instance(DBInstanceIdentifier=name, Engine="postgres", EngineVersion="16.3",
                                   DBInstanceClass="db.t3.micro", AllocatedStorage=20,
                                   MasterUsername="verifier", MasterUserPassword=password, DBName="orch")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def make_cache():
        try:
            cache.create_cache_cluster(CacheClusterId=name, Engine="redis", CacheNodeType="cache.t3.micro",
                                       NumCacheNodes=1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=make_db), threading.Thread(target=make_cache)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise EnvironmentDown(f"provisioning failed: {errors!r}")
    deadline = time.time() + 400
    db = cc = None
    while time.time() < deadline:
        db = rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
        cc = cache.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)["CacheClusters"][0]
        if db["DBInstanceStatus"] == "available" and cc["CacheClusterStatus"] == "available":
            break
        time.sleep(2)
    else:
        raise EnvironmentDown("RDS/ElastiCache never became available")
    ep = db["Endpoint"]
    node = cc["CacheNodes"][0]["Endpoint"]
    stack = Stack(name, f"postgresql://verifier:{password}@{ep['Address']}:{ep['Port']}/orch",
                  f"redis://{node['Address']}:{node['Port']}/0")
    for _ in range(60):
        try:
            psycopg.connect(stack.pg_dsn, connect_timeout=3).close()
            redis.Redis.from_url(stack.valkey_url, socket_timeout=3).ping()
            return stack
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise EnvironmentDown("engines reported available but are not reachable")


def teardown(stack: Stack | None) -> None:
    if stack is None:
        return
    cfg = Config(read_timeout=120, retries={"max_attempts": 1})
    for call in (lambda: boto3.client("rds", config=cfg).delete_db_instance(
                     DBInstanceIdentifier=stack.name, SkipFinalSnapshot=True),
                 lambda: boto3.client("elasticache", config=cfg).delete_cache_cluster(CacheClusterId=stack.name)):
        try:
            call()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------------------------
# processes

class Procs:
    def __init__(self, stack: Stack, workdir: Path, seam_path: str, events_path: str):
        self.stack = stack
        self.workdir = workdir
        self.seam_path = seam_path
        self.events_path = events_path
        self.fleet_path = workdir / "fleet-src"
        shutil.copytree(HERE / "fleet", self.fleet_path / "fleet")
        self.roles: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()
        self.restarts: list[dict[str, Any]] = []

    def _env(self, pythonpath: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
        env.update({
            "PYTHONPATH": pythonpath, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PG_DSN": self.stack.pg_dsn, "VALKEY_URL": self.stack.valkey_url,
            "ORCH_PORT": str(PORT), "ORCH_URL": ORCH_URL,
            "LEASE_TTL_S": str(LEASE_TTL_S), "SLOT_WAIT_S": str(SLOT_WAIT_S),
            "REPOSSESS_LOOP_S": str(REPOSSESS_LOOP_S),
            "LEASE_DURATION_S": str(LEASE_TTL_S), "WORKER_POLL_S": str(WORKER_POLL_S),
            "SEAM_SOCKET": self.seam_path, "FLEET_EVENTS": self.events_path,
        })
        return env

    def start(self, role: str) -> subprocess.Popen:
        module = {"orchestrator": "orchestrator.server", "repossessor": "orchestrator.repossessor",
                  "worker": "fleet.worker"}[role]
        pythonpath = str(self.fleet_path) if role == "worker" else "/app"
        cwd = tempfile.mkdtemp(prefix=f"{role}-", dir=self.workdir)
        log = open(LOGS / f"{role}.log", "a", encoding="utf-8")
        log.write(f"\n===== start {role} at {time.time():.3f}\n")
        log.flush()
        proc = subprocess.Popen([sys.executable, "-m", module], cwd=cwd, env=self._env(pythonpath),
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        with self.lock:
            self.roles[role] = proc
        return proc

    def role_of(self, pid: int) -> str | None:
        with self.lock:
            for role, proc in self.roles.items():
                if proc.pid == pid and role != "worker":
                    return role
        # a child of a server process
        try:
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except Exception:  # noqa: BLE001
            return None
        with self.lock:
            for role, proc in self.roles.items():
                if proc.pid == ppid and role != "worker":
                    return role
        return None

    def kill_role(self, role: str) -> None:
        with self.lock:
            proc = self.roles.get(role)
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)

    def stop_all(self) -> None:
        for role in list(self.roles):
            self.kill_role(role)


class SeamServer:
    """Accept pause-point announcements; ack them, or kill the announcing server process."""

    def __init__(self, path: str):
        self.path = path
        self.log: list[dict[str, Any]] = []
        self.decide: Callable[[dict[str, Any]], str] = lambda msg: "ack"
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(64)
        self._lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            buf = b""
            conn.settimeout(10)
            try:
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                return
            try:
                msg = json.loads(buf.decode() or "{}")
            except ValueError:
                msg = {"raw": buf.decode(errors="replace")}
            msg["t"] = time.time()
            with self._lock:
                action = self.decide(msg)
                msg["action"] = action
                self.log.append(msg)
            if action == "ack":
                try:
                    conn.sendall(b"go\n")
                except OSError:
                    pass
            # "kill": the decider already killed the process; never answer


# --------------------------------------------------------------------------------------------
# observation helpers

def http(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ORCH_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode(errors="replace")


class Observer:
    def __init__(self, stack: Stack):
        self.dsn = stack.pg_dsn
        self.conn = psycopg.connect(stack.pg_dsn, autocommit=True, row_factory=dict_row, connect_timeout=5)
        self.kv = redis.Redis.from_url(stack.valkey_url, decode_responses=True, socket_timeout=5)
        self.lock = threading.Lock()

    def q(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            for attempt in range(3):
                try:
                    return self.conn.execute(sql, args).fetchall()
                except psycopg.OperationalError:
                    if attempt == 2:
                        raise
                    self.conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=5)
        return []

    def states(self, run_id: str) -> list[dict[str, Any]]:
        return self.q("SELECT type, name, lease_id, extract(epoch FROM ts)::float8 AS ts FROM flow_run_state "
                      "WHERE flow_run_id = %s ORDER BY id", (run_id,))

    def run(self, run_id: str) -> dict[str, Any]:
        rows = self.q("SELECT * FROM flow_run WHERE id = %s", (run_id,))
        return rows[0] if rows else {}

    def attempts(self, run_id: str) -> list[dict[str, Any]]:
        return self.q("SELECT attempt, lease_id::text AS lease_id, outcome, "
                      "extract(epoch FROM body_start)::float8 AS start, extract(epoch FROM body_end)::float8 AS end "
                      "FROM attempt_log WHERE flow_run_id = %s ORDER BY id", (run_id,))

    def lease_present(self, lease_id: str) -> bool:
        return bool(self.kv.exists(LEASE_PREFIX + lease_id)) or self.kv.zscore(EXPIRATIONS, lease_id) is not None


def wait_for(pred: Callable[[], Any], timeout: float, interval: float = 0.1) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = pred()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value
        time.sleep(interval)
    return None


def read_events(path: str) -> list[dict[str, Any]]:
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return out


# --------------------------------------------------------------------------------------------
# the case

@dataclass
class Record:
    ok_start: bool = False
    errors: list[str] = field(default_factory=list)
    t0: float = 0.0
    end: float = 0.0
    deployments: dict[str, dict[str, str]] = field(default_factory=dict)   # D1.. -> {id, limit_id}
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)          # R1.. -> {id, dep}
    marks: dict[str, Any] = field(default_factory=dict)
    samples: list[tuple[float, dict[str, int]]] = field(default_factory=list)
    states: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    attempts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    final_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    seams: list[dict[str, Any]] = field(default_factory=list)
    renews: dict[str, Any] = field(default_factory=dict)
    final_slots: dict[str, int] = field(default_factory=dict)
    final_valkey: dict[str, Any] = field(default_factory=dict)

    def rid(self, name: str) -> str:
        return self.runs[name]["id"]


PLAN = [
    # name, deployment, retries, retry_delay, attempts, otherwise, delay
    ("R1", "D1", 1, RETRY_DELAY_S, [(1, "fail"), (16, "fail")], (3, "complete"), 0),
    ("R2", "D1", 0, 0, [], (3, "complete"), 0),
    ("R3", "D1", 0, 0, [], (3, "complete"), 0),
    ("R4", "D2", 1, RETRY_DELAY_S, [(1, "fail"), (60, "fail")], (3, "complete"), 0),
    ("R5", "D2", 0, 0, [], (3, "complete"), 0),
    ("R6", "D3", 0, 0, [], (60, "complete"), 0),
    ("R7", "D3", 0, 0, [], (20, "complete"), 0),
    ("R8", "D3", 0, 0, [], (3, "complete"), 0),
    ("R9", "D4", 0, 0, [], (60, "complete"), D4_DELAY_S),
    ("R10", "D4", 0, 0, [], (20, "complete"), D4_DELAY_S),
    ("R11", "D4", 0, 0, [], (3, "complete"), D4_DELAY_S),
]


def _seed(obs: Observer, rec: Record) -> None:
    tag = uuid.uuid4().hex[:6]
    with obs.lock, obs.conn.transaction():
        for dep in ("D1", "D2", "D3", "D4"):
            dep_id, limit_id = str(uuid.uuid4()), str(uuid.uuid4())
            obs.conn.execute('INSERT INTO concurrency_limit_v2 (id, name, "limit") VALUES (%s, %s, 1)',
                             (limit_id, f"deployment:{dep_id}"))
            obs.conn.execute("INSERT INTO deployment (id, name, concurrency_limit_id, collision_strategy) "
                             "VALUES (%s, %s, %s, 'ENQUEUE')", (dep_id, f"{tag}-{dep.lower()}-{uuid.uuid4().hex[:4]}", limit_id))
            rec.deployments[dep] = {"id": dep_id, "limit_id": limit_id}
        for name, dep, retries, delay, attempts, otherwise, due in PLAN:
            run_id = str(uuid.uuid4())
            params = {"attempts": [{"seconds": s, "outcome": o} for s, o in attempts],
                      "otherwise": {"seconds": otherwise[0], "outcome": otherwise[1]}}
            obs.conn.execute(
                "INSERT INTO flow_run (id, deployment_id, name, parameters, retries, retry_delay_s, state_type, "
                "state_name, scheduled_time) VALUES (%s, %s, %s, %s, %s, %s, 'SCHEDULED', 'Scheduled', "
                "clock_timestamp() + make_interval(secs => %s))",
                (run_id, rec.deployments[dep]["id"], f"{tag}-{name.lower()}", json.dumps(params), retries, delay, due))
            obs.conn.execute("INSERT INTO flow_run_state (flow_run_id, type, name, scheduled_time) "
                             "SELECT id, 'SCHEDULED', 'Scheduled', scheduled_time FROM flow_run WHERE id = %s",
                             (run_id,))
            rec.runs[name] = {"id": run_id, "dep": dep}


def _supervisor_pid(events_path: str, run_id: str) -> int | None:
    spawns = [e for e in read_events(events_path) if e.get("ev") == "spawn" and e.get("run") == run_id]
    return int(spawns[-1]["supervisor"]) if spawns else None


def _lease_of_attempt(obs: Observer, run_id: str, attempt: int | None = None) -> str | None:
    rows = obs.attempts(run_id)
    if attempt is not None:
        rows = [r for r in rows if r["attempt"] == attempt]
    rows = [r for r in rows if r["lease_id"]]
    if rows:
        return rows[-1]["lease_id"]
    states = [s for s in obs.states(run_id) if s["type"] == "RUNNING" and s["lease_id"]]
    return str(states[-1]["lease_id"]) if states else None


def run_case() -> Record:
    rec = Record()
    LOGS.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="verifier-"))
    seam_path = str(workdir / "seam.sock")
    events_path = str(workdir / "events.jsonl")
    stack = provision()
    procs: Procs | None = None
    stop = threading.Event()
    try:
        seams = SeamServer(seam_path)
        procs = Procs(stack, workdir, seam_path, events_path)
        procs.start("orchestrator")
        procs.start("repossessor")
        healthy = wait_for(lambda: http("GET", "/health", timeout=3)[0] == 200, STARTUP_TIMEOUT_S, 0.5)
        if not healthy:
            rec.errors.append(f"orchestrator never answered GET /health with 200 within {STARTUP_TIMEOUT_S}s")
            return rec
        time.sleep(1.0)  # let the repossessor finish its own start-up
        obs = Observer(stack)
        with obs.lock:
            obs.conn.execute(BASE_DDL)
        _seed(obs, rec)
        rec.ok_start = True

        # -- samplers ----------------------------------------------------------------------
        limit_to_dep = {v["limit_id"]: k for k, v in rec.deployments.items()}

        def sampler():
            while not stop.is_set():
                try:
                    rows = obs.q("SELECT id::text AS id, active_slots FROM concurrency_limit_v2")
                    rec.samples.append((time.time(), {limit_to_dep[r["id"]]: r["active_slots"]
                                                      for r in rows if r["id"] in limit_to_dep}))
                except Exception as exc:  # noqa: BLE001
                    rec.errors.append(f"sampler: {exc}")
                stop.wait(0.25)

        threading.Thread(target=sampler, daemon=True).start()

        # -- pause-point decisions ---------------------------------------------------------
        d4 = {"lease": None, "armed": False, "killed": None}

        def decide(msg: dict[str, Any]) -> str:
            role = procs.role_of(int(msg.get("pid") or 0))
            msg["role"] = role
            if (d4["armed"] and d4["killed"] is None and role is not None
                    and str(msg.get("lease_id")) == d4["lease"]):
                d4["killed"] = {"t": time.time(), "role": role, "seam": msg.get("seam")}
                procs.kill_role(role)
                return "kill"
            return "ack"

        seams.decide = decide
        rec.t0 = time.time()
        procs.start("worker")
        R = rec.rid

        def running_count(name: str) -> int:
            return sum(1 for s in obs.states(R(name)) if s["type"] == "RUNNING")

        # -- D2: SIGTERM the supervisor of R4's second attempt ------------------------------
        def drive_d2():
            if not wait_for(lambda: running_count("R4") >= 2, 45):
                rec.marks["d2_error"] = "R4 never started a second attempt"
                return
            rec.marks["d2_attempt2_running"] = time.time()
            lease = _lease_of_attempt(obs, R("R4"), 2) or _lease_of_attempt(obs, R("R4"))
            rec.marks["L4"] = lease
            time.sleep(SIGTERM_AFTER_S)
            pid = _supervisor_pid(events_path, R("R4"))
            if pid is None:
                rec.marks["d2_error"] = "no supervisor recorded for R4"
                return
            rec.marks["d2_sigterm"] = time.time()
            os.kill(pid, signal.SIGTERM)

            def rescheduled():
                states = obs.states(R("R4"))
                idx = [i for i, s in enumerate(states) if s["type"] == "RUNNING"]
                if len(idx) < 2:
                    return None
                later = [s for s in states[idx[1] + 1:] if s["type"] != "RUNNING"]
                return later[0] if later else None

            first = wait_for(rescheduled, 10, 0.05)
            if not first:
                rec.marks["d2_error"] = "R4 left no state after the SIGTERM"
                return
            rec.marks["d2_resched"] = first
            if lease:
                # By now the reschedule's request has long returned: the lease must be dead.
                time.sleep(max(0.0, first["ts"] + RELEASE_PROBE_S - time.time()))
                rec.marks["d2_probe_at"] = time.time()
                rec.marks["d2_probe"] = http("POST", f"/leases/{lease}/renew", {"lease_duration": LEASE_TTL_S})[0]

        # -- D3: SIGKILL R6's process group; after R7 runs, force R6 Crashed ---------------
        def drive_d3():
            if not wait_for(lambda: running_count("R6") >= 1, 45):
                rec.marks["d3_error"] = "R6 never ran"
                return
            rec.marks["L6"] = wait_for(lambda: _lease_of_attempt(obs, R("R6")), 5)
            time.sleep(SIGKILL_AFTER_S)
            pid = _supervisor_pid(events_path, R("R6"))
            rec.marks["d3_kill"] = time.time()
            if pid:
                os.killpg(pid, signal.SIGKILL)
            if wait_for(lambda: running_count("R7") >= 1, 25):
                rec.marks["d3_r7_running"] = time.time()
            rec.marks["d3_crash_sent"] = time.time()
            rec.marks["d3_crash"] = http("POST", f"/flow_runs/{R('R6')}/set_state",
                                         {"type": "CRASHED", "name": "Crashed", "force": True,
                                          "message": "infrastructure lost"})

        # -- D4: SIGKILL R9; kill whichever server process reaps its lease, at its pause point
        def drive_d4():
            if not wait_for(lambda: running_count("R9") >= 1, D4_DELAY_S + 45):
                rec.marks["d4_error"] = "R9 never ran"
                return
            lease = wait_for(lambda: _lease_of_attempt(obs, R("R9")), 5)
            rec.marks["L9"] = lease
            time.sleep(SIGKILL_AFTER_S)
            pid = _supervisor_pid(events_path, R("R9"))
            d4["lease"] = str(lease)
            d4["armed"] = True
            rec.marks["d4_kill"] = time.time()
            if pid:
                os.killpg(pid, signal.SIGKILL)
            if wait_for(lambda: d4["killed"], LEASE_TTL_S + 12, 0.05):
                rec.marks["d4_seam_kill"] = d4["killed"]
                time.sleep(RESTART_AFTER_S)
                procs.start(d4["killed"]["role"])
                rec.marks["d4_restart"] = time.time()
                if d4["killed"]["role"] == "orchestrator":
                    wait_for(lambda: http("GET", "/health", timeout=3)[0] == 200, STARTUP_TIMEOUT_S, 0.5)
                time.sleep(max(0.0, d4["killed"]["t"] + LATE_CRASH_AFTER_S - time.time()))
            else:
                rec.marks["d4_error"] = "no server process announced a pause point for R9's expired lease"
            d4["armed"] = False
            rec.marks["d4_crash_sent"] = time.time()
            rec.marks["d4_crash"] = http("POST", f"/flow_runs/{R('R9')}/set_state",
                                         {"type": "CRASHED", "name": "Crashed", "force": True,
                                          "message": "infrastructure lost"})

        drivers = []
        for name, fn in (("d2", drive_d2), ("d3", drive_d3), ("d4", drive_d4)):
            def wrapped(fn=fn, name=name):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    rec.errors.append(f"driver {name}: {traceback.format_exc()}")
            t = threading.Thread(target=wrapped, daemon=True)
            t.start()
            drivers.append(t)

        def quiet():
            if any(t.is_alive() for t in drivers):
                return False
            rows = obs.q("SELECT state_type FROM flow_run WHERE id = ANY(%s::uuid[])",
                         ([r["id"] for r in rec.runs.values()],))
            return len(rows) == len(rec.runs) and all(r["state_type"] in TERMINAL for r in rows)

        if not wait_for(quiet, CASE_TIMEOUT_S, 0.5):
            rec.errors.append(f"the workload did not finish within {CASE_TIMEOUT_S}s")
        time.sleep(2 * REPOSSESS_LOOP_S + 1.5)
        rec.end = time.time()

        # -- direct renewals of leases that must be dead -----------------------------------
        for key in ("L4", "L6", "L9"):
            lease = rec.marks.get(key)
            if lease:
                rec.renews[key] = http("POST", f"/leases/{lease}/renew", {"lease_duration": LEASE_TTL_S})[0]
        l1 = _lease_of_attempt(obs, R("R1"), 1)
        rec.marks["L1"] = l1
        if l1:
            rec.renews["L1"] = http("POST", f"/leases/{l1}/renew", {"lease_duration": LEASE_TTL_S})[0]
        rec.renews["unknown"] = http("POST", f"/leases/{uuid.uuid4()}/renew", {"lease_duration": LEASE_TTL_S})[0]
        time.sleep(0.3)
        stop.set()

        # -- collect -----------------------------------------------------------------------
        for name, info in rec.runs.items():
            rec.states[name] = obs.states(info["id"])
            rec.attempts[name] = obs.attempts(info["id"])
            rec.final_runs[name] = obs.run(info["id"])
        rows = obs.q("SELECT id::text AS id, active_slots FROM concurrency_limit_v2")
        rec.final_slots = {limit_to_dep[r["id"]]: r["active_slots"] for r in rows if r["id"] in limit_to_dep}
        rec.final_valkey = {
            "zcard": obs.kv.zcard(EXPIRATIONS),
            "lease_keys": sorted(obs.kv.scan_iter(LEASE_PREFIX + "*")),
            "renewed_keys": {k: bool(obs.kv.exists(LEASE_PREFIX + str(rec.marks.get(k))))
                             for k in ("L1", "L4", "L6", "L9") if rec.marks.get(k)},
        }
        rec.events = read_events(events_path)
        rec.seams = list(seams.log)
        return rec
    except EnvironmentDown:
        raise
    except Exception:  # noqa: BLE001
        rec.errors.append(traceback.format_exc())
        return rec
    finally:
        stop.set()
        # stop the fleet first (its supervisors live in their own sessions), then the servers
        for e in read_events(events_path):
            if e.get("ev") == "spawn":
                try:
                    os.killpg(int(e["supervisor"]), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if procs is not None:
            procs.stop_all()
        try:
            shutil.copy(events_path, LOGS / "fleet-events.jsonl")
        except OSError:
            pass
        try:
            (LOGS / "record.json").write_text(json.dumps(rec.__dict__, default=str, indent=1))
        except Exception:  # noqa: BLE001
            pass
        teardown(stack)
