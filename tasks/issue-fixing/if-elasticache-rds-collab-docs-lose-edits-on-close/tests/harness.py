"""Verifier harness: engines, the Valkey proxy, the peer instance, the lock monitor, docserver processes.

Nothing here imports solver code. Expectations are derived from what the harness itself sent and
what Postgres committed.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3
import psycopg
import pycrdt
import redis
from botocore.config import Config

LOGS = Path("/logs/verifier")
HERE = Path(__file__).resolve().parent
CONFIG = Config(read_timeout=600, retries={"max_attempts": 1})

LOCK_PX_MS = 1000
HELD_LOCK_PX_MS = 6000
PEER_TX_MIN_HOLD_S = 1.5
BARRIER_TIMEOUT_S = 5.0
BARRIER_POLL_S = 0.02
EDIT_ACK_TIMEOUT_S = 8.0
UNLOAD_DEADLINE_S = 15.0
UNLOAD_POLL_S = 0.05
VALKEY_OUTAGE_S = 30.0
LOCK_MONITOR_POLL_S = 0.025
QUIESCE_S = 2.0
COMPACT_PX_MS = 10000
COMPACT_RETRY_S = 0.05


def log(message: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with contextlib.suppress(OSError), (LOGS / "harness.log").open("a") as handle:
        handle.write(line + "\n")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def edits_of(state: bytes | None) -> set[str]:
    if state is None:
        return set()
    doc = pycrdt.Doc()
    try:
        doc.apply_update(bytes(state))
        return set(doc.get("edits", type=pycrdt.Map).keys())
    except Exception as error:  # an undecodable state holds no edits
        log(f"undecodable state ({len(state)} bytes): {error!r}")
        return set()


def state_with(edit_ids: set[str], base: bytes | None = None) -> bytes:
    doc = pycrdt.Doc()
    if base:
        doc.apply_update(bytes(base))
    edits = doc.get("edits", type=pycrdt.Map)
    for edit_id in sorted(edit_ids):
        edits[edit_id] = edit_id
    return doc.get_update()


class EnvironmentFailure(Exception):
    pass


# ── engines ─────────────────────────────────────────────────────────────────────────


class Engines:
    def __init__(self) -> None:
        suffix = secrets.token_hex(3)
        self.name = f"verify-{suffix}"
        self.master = ("verifier", "vm-" + secrets.token_hex(8))
        self.docserver_password = "ds-" + secrets.token_hex(8)
        self.peer_password = "pr-" + secrets.token_hex(8)
        self.compact_password = "dc-" + secrets.token_hex(8)
        self.pg: tuple[str, int] | None = None
        self.valkey: tuple[str, int] | None = None

    def _rds(self) -> None:
        rds = boto3.client("rds", config=CONFIG)
        rds.create_db_instance(
            DBInstanceIdentifier=self.name, Engine="postgres", EngineVersion="16.3", DBInstanceClass="db.t3.micro",
            AllocatedStorage=20, DBName="docs", MasterUsername=self.master[0], MasterUserPassword=self.master[1],
        )
        deadline = time.monotonic() + 400
        while time.monotonic() < deadline:
            instance = rds.describe_db_instances(DBInstanceIdentifier=self.name)["DBInstances"][0]
            if instance["DBInstanceStatus"] == "available" and instance.get("Endpoint"):
                self.pg = (instance["Endpoint"]["Address"], int(instance["Endpoint"]["Port"]))
                return
            time.sleep(2)
        raise EnvironmentFailure("RDS instance did not become available")

    def _valkey(self) -> None:
        elasticache = boto3.client("elasticache", config=CONFIG)
        elasticache.create_cache_cluster(CacheClusterId=self.name, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1)
        deadline = time.monotonic() + 400
        while time.monotonic() < deadline:
            cluster = elasticache.describe_cache_clusters(CacheClusterId=self.name, ShowCacheNodeInfo=True)["CacheClusters"][0]
            nodes = cluster.get("CacheNodes") or []
            if cluster["CacheClusterStatus"] == "available" and nodes and nodes[0].get("Endpoint"):
                self.valkey = (nodes[0]["Endpoint"]["Address"], int(nodes[0]["Endpoint"]["Port"]))
                return
            time.sleep(2)
        raise EnvironmentFailure("ElastiCache cluster did not become available")

    def create(self) -> None:
        errors: list[BaseException] = []

        def run(target):
            try:
                target()
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=run, args=(t,)) for t in (self._rds, self._valkey)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise EnvironmentFailure(f"engine creation failed: {errors!r}")
        self.master_conn()  # reachable
        self.valkey_client().ping()
        log(f"engines: postgres {self.pg}, valkey {self.valkey}")

    def dsn(self, user: str, password: str, via: tuple[str, int] | None = None) -> str:
        host, port = via or self.pg
        return f"postgresql://{user}:{password}@{host}:{port}/docs"

    def master_conn(self) -> psycopg.Connection:
        last = None
        for _ in range(30):
            try:
                return psycopg.connect(self.dsn(*self.master), autocommit=True, connect_timeout=5)
            except psycopg.OperationalError as error:
                last = error
                time.sleep(1)
        raise EnvironmentFailure(f"postgres unreachable: {last}")

    def valkey_client(self) -> redis.Redis:
        host, port = self.valkey
        last = None
        for _ in range(30):
            try:
                client = redis.Redis(host=host, port=port, socket_timeout=5, socket_connect_timeout=5)
                client.ping()
                return client
            except redis.RedisError as error:
                last = error
                time.sleep(1)
        raise EnvironmentFailure(f"valkey unreachable: {last}")

    def apply_schema(self) -> None:
        with self.master_conn() as conn:
            conn.execute((HERE / "schema.sql").read_text())
            conn.execute(f"ALTER ROLE docserver PASSWORD '{self.docserver_password}'")
            conn.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'peer') THEN CREATE ROLE peer LOGIN; END IF; END $$"
            )
            conn.execute(f"ALTER ROLE peer PASSWORD '{self.peer_password}'")
            conn.execute("GRANT SELECT, INSERT, UPDATE ON documents TO peer")
            conn.execute(f"ALTER ROLE doccompact PASSWORD '{self.compact_password}'")
            conn.execute("GRANT CONNECT ON DATABASE docs TO docserver, peer, doccompact")


# ── TCP proxies (Valkey and Postgres, as the docservers reach them) ───────────────────


class TcpProxy:
    """127.0.0.1:<port> -> an engine endpoint. ``refuse()`` closes every live socket and the listener."""

    def __init__(self, label: str, upstream: tuple[str, int]) -> None:
        self.label = label
        self.upstream = upstream
        self.port = free_port()
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self.forward()

    def forward(self) -> None:
        with self._lock:
            if self._listener is not None:
                return
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(128)
            self._listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()
        log(f"{self.label} proxy 127.0.0.1:{self.port} forwarding to {self.upstream}")

    def refuse(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
            sockets, self._sockets = self._sockets, set()
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            listener.close()
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        log(f"{self.label} proxy refusing")

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.upstream, timeout=5)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            with self._lock:
                if self._listener is not listener:
                    client.close()
                    upstream.close()
                    continue
                self._sockets.update((client, upstream))
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(source, sink), daemon=True).start()

    def _pipe(self, source: socket.socket, sink: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while chunk := source.recv(65536):
                sink.sendall(chunk)
        for sock in (source, sink):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        with self._lock:
            self._sockets.discard(source)


# ── the lock monitor ────────────────────────────────────────────────────────────────


class LockMonitor:
    """GETs every lock key the peer holds; any other value before its release or PX deadline is a theft."""

    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self.watches: dict[str, tuple[bytes, float]] = {}
        self.violations: list[str] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def watch(self, key: str, token: bytes, until: float) -> None:
        with self._lock:
            self.watches[key] = (token, until)

    def unwatch(self, key: str) -> None:
        with self._lock:
            self.watches.pop(key, None)

    def _run(self) -> None:
        while True:
            with self._lock:
                watches = dict(self.watches)
            for key, (token, until) in watches.items():
                if time.monotonic() >= until:
                    self.unwatch(key)
                    continue
                try:
                    value = self.client.get(key)
                except redis.RedisError:
                    continue
                with self._lock:
                    still = self.watches.get(key) == (token, until)
                if still and value != token and time.monotonic() < until:
                    self.violations.append(f"{key} read {value!r} while the peer held it with {token!r}")
                    self.unwatch(key)
            time.sleep(LOCK_MONITOR_POLL_S)


class PeerLock:
    RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

    def __init__(self, harness: "Harness", key: str, px_ms: int) -> None:
        self.harness = harness
        self.key = key
        self.token = ("peer-" + secrets.token_hex(8)).encode()
        start = time.monotonic()
        self.held = bool(harness.valkey.set(key, self.token, nx=True, px=px_ms))
        if self.held:
            harness.monitor.watch(key, self.token, start + px_ms / 1000 - 0.1)

    def release(self) -> None:
        if self.held:
            self.harness.monitor.unwatch(self.key)
            with contextlib.suppress(redis.RedisError):
                self.harness.valkey.eval(self.RELEASE, 1, self.key, self.token)
            self.held = False


# ── the platform's compaction job ───────────────────────────────────────────────────


class Compactor:
    """doccompact on one document: under the store lock, read the row, re-encode it, write it back.

    It relies on the lock for exclusion and does not re-check the version it read (README, "Row
    compaction"). ``run`` tries the lock every COMPACT_RETRY_S for up to ``acquire_within_s``.
    """

    RELEASE = PeerLock.RELEASE

    def __init__(self, harness: "Harness", doc: str) -> None:
        self.harness = harness
        self.doc = doc
        self.key = harness.lock_key(doc)
        self.token = ("doccompact-" + secrets.token_hex(8)).encode()
        self.acquired_at: float | None = None
        self.read_version: int | None = None
        self.state: bytes | None = None

    def try_acquire(self) -> bool:
        start = time.monotonic()
        if self.harness.valkey.set(self.key, self.token, nx=True, px=COMPACT_PX_MS):
            self.acquired_at = start
            self.harness.monitor.watch(self.key, self.token, start + COMPACT_PX_MS / 1000 - 0.1)
            with psycopg.connect(self.harness.engines.dsn("doccompact", self.harness.engines.compact_password), connect_timeout=5) as conn:
                row = conn.execute("SELECT state, version FROM documents WHERE name = %s", (self.doc,)).fetchone()
            self.state, self.read_version = bytes(row[0]), row[1]
            log(f"doccompact took {self.key} and read {self.doc} v{self.read_version}")
            return True
        return False

    def run(self, hold_s: float, acquire_within_s: float = 0.0) -> bool:
        """Compact the document once; False if the lock never came free in time."""
        deadline = time.monotonic() + acquire_within_s
        while self.acquired_at is None and not self.try_acquire():
            if time.monotonic() >= deadline:
                log(f"doccompact gave up on {self.doc}: {self.key} stayed held")
                return False
            time.sleep(COMPACT_RETRY_S)
        remaining = hold_s - (time.monotonic() - self.acquired_at)
        if remaining > 0:
            time.sleep(remaining)
        doc = pycrdt.Doc()
        doc.apply_update(self.state)
        with psycopg.connect(self.harness.engines.dsn("doccompact", self.harness.engines.compact_password), connect_timeout=5) as conn:
            conn.execute("UPDATE documents SET state = %s, version = version + 1 WHERE name = %s", (doc.get_update(), self.doc))
        log(f"doccompact rewrote {self.doc} from v{self.read_version}")
        self.harness.monitor.unwatch(self.key)
        with contextlib.suppress(redis.RedisError):
            self.harness.valkey.eval(self.RELEASE, 1, self.key, self.token)
        return True


# ── the peer instance's row write ───────────────────────────────────────────────────


class PeerTx:
    """Another instance storing the document: SELECT ... FOR UPDATE, merge its edit, conditional UPDATE."""

    def __init__(self, harness: "Harness", doc: str) -> None:
        self.harness = harness
        self.doc = doc
        self.conn = psycopg.connect(harness.engines.dsn("peer", harness.engines.peer_password), connect_timeout=5)
        self.pid = self.conn.info.backend_pid
        row = self.conn.execute("SELECT state, version FROM documents WHERE name = %s FOR UPDATE", (doc,)).fetchone()
        self.state, self.version = bytes(row[0]), row[1]
        self.started = time.monotonic()

    def blocked(self) -> list[int]:
        with self.harness.engines.master_conn() as conn:
            rows = conn.execute(
                "SELECT pid FROM pg_stat_activity WHERE usename = 'docserver' AND %s = ANY(pg_blocking_pids(pid))", (self.pid,)
            ).fetchall()
        return [row[0] for row in rows]

    def wait_blocked(self, timeout_s: float) -> list[int]:
        deadline = time.monotonic() + timeout_s
        with self.harness.engines.master_conn() as conn:
            while True:
                rows = conn.execute(
                    "SELECT pid FROM pg_stat_activity WHERE usename = 'docserver' AND %s = ANY(pg_blocking_pids(pid))",
                    (self.pid,),
                ).fetchall()
                if rows or time.monotonic() >= deadline:
                    return [row[0] for row in rows]
                time.sleep(BARRIER_POLL_S)

    def commit_edit(self, edit_id: str) -> None:
        state = state_with({edit_id}, self.state)
        cursor = self.conn.execute(
            "UPDATE documents SET state = %s, version = version + 1 WHERE name = %s AND version = %s",
            (state, self.doc, self.version),
        )
        assert cursor.rowcount == 1, "peer lost its own row lock"
        self.conn.commit()
        self.conn.close()
        self.harness.acked[self.doc].add(edit_id)
        log(f"peer committed {edit_id} on {self.doc} v{self.version + 1}")

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()


# ── docserver processes ─────────────────────────────────────────────────────────────


class Docserver:
    def __init__(self, harness: "Harness", instance_id: str) -> None:
        self.harness = harness
        self.instance_id = instance_id
        self.process: subprocess.Popen | None = None
        self.port = 0
        self.crashes: list[str] = []

    def start(self) -> None:
        self.port = free_port()
        env = {
            **os.environ,
            "PYTHONPATH": "/app",
            "PG_DSN": self.harness.engines.dsn("docserver", self.harness.engines.docserver_password, ("127.0.0.1", self.harness.pg_proxy.port)),
            "VALKEY_URL": f"redis://127.0.0.1:{self.harness.proxy.port}/0",
            "KEY_PREFIX": self.harness.prefix,
        }
        env.pop("SEAM_SOCKET", None)
        logfile = (LOGS / f"docserver-{self.instance_id}.log").open("ab")
        self.process = subprocess.Popen(
            ["python3", "-m", "docserver", "--instance-id", self.instance_id, "--http-port", str(self.port)],
            cwd=tempfile.mkdtemp(prefix=f"docserver-{self.instance_id}-"), env=env,
            stdout=logfile, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"docserver {self.instance_id} exited with {self.process.returncode} at startup")
            with contextlib.suppress(Exception):
                if self.request("GET", "/healthz", timeout=1)[0] == 200:
                    return
            time.sleep(0.2)
        raise AssertionError(f"docserver {self.instance_id} did not answer /healthz within 30 s")

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def ensure(self) -> None:
        if not self.alive():
            if self.process is not None:
                self.crashes.append(f"{self.instance_id} exited with {self.process.returncode}")
                log(f"docserver {self.instance_id} died; restarting")
            self.start()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            raw = error.read()
            with contextlib.suppress(ValueError):
                return error.code, json.loads(raw)
            return error.code, {}


# ── the harness ─────────────────────────────────────────────────────────────────────


class Harness:
    def __init__(self) -> None:
        LOGS.mkdir(parents=True, exist_ok=True)
        self.engines = Engines()
        self.engines.create()
        self.engines.apply_schema()
        self.valkey = self.engines.valkey_client()
        self.proxy = TcpProxy("valkey", self.engines.valkey)
        self.pg_proxy = TcpProxy("postgres", self.engines.pg)
        self.monitor = LockMonitor(self.engines.valkey_client())
        self.prefix = "k" + secrets.token_hex(2)
        self.acked: dict[str, set[str]] = {}
        self.instances = {name: Docserver(self, name) for name in ("I1", "I2")}
        for server in self.instances.values():
            server.start()

    def ensure_docservers(self) -> None:
        for server in self.instances.values():
            server.ensure()

    def close_all(self) -> None:
        for server in self.instances.values():
            server.stop()
        self.proxy.refuse()
        self.pg_proxy.refuse()

    # documents

    def new_doc(self, label: str) -> str:
        doc = f"{label}-{secrets.token_hex(3)}"
        seed = f"{doc}:seed"
        with self.engines.master_conn() as conn:
            conn.execute("INSERT INTO documents (name, state, version) VALUES (%s, %s, 1)", (doc, state_with({seed})))
        self.acked[doc] = {seed}
        return doc

    def row(self, doc: str) -> tuple[int | None, set[str]]:
        with self.engines.master_conn() as conn:
            row = conn.execute("SELECT version, state FROM documents WHERE name = %s", (doc,)).fetchone()
        return (None, set()) if row is None else (row[0], edits_of(row[1]))

    def history(self, doc: str) -> list[tuple[int, int, set[str], str]]:
        with self.engines.master_conn() as conn:
            rows = conn.execute("SELECT id, version, state, writer FROM documents_history WHERE name = %s ORDER BY id", (doc,)).fetchall()
        return [(row[0], row[1], edits_of(row[2]), row[3]) for row in rows]

    def history_problems(self, doc: str) -> list[str]:
        problems = []
        entries = self.history(doc)
        for previous, current in zip(entries, entries[1:]):
            if current[1] <= previous[1]:
                problems.append(f"{doc}: history version {current[1]} (by {current[3]}) follows version {previous[1]}")
            lost = previous[2] - current[2]
            if lost:
                problems.append(
                    f"{doc}: committed version {current[1]} (by {current[3]}) dropped {sorted(lost)} that version {previous[1]} (by {previous[3]}) held"
                )
        return problems

    # docserver calls

    def open(self, instance: str, doc: str) -> None:
        status, body = self.instances[instance].request("POST", "/open", {"doc": doc}, timeout=15)
        assert status == 200, f"POST /open {doc} on {instance}: {status} {body}"

    def edit(self, instance: str, doc: str, edit_id: str, acked_by: set[str] | None = None) -> None:
        started = time.monotonic()
        try:
            status, body = self.instances[instance].request("POST", "/edit", {"doc": doc, "edit_id": edit_id}, timeout=EDIT_ACK_TIMEOUT_S)
        except Exception as error:
            raise AssertionError(f"POST /edit {edit_id} on {instance} not acknowledged within {EDIT_ACK_TIMEOUT_S:.0f} s: {error!r}") from None
        assert status == 200, f"POST /edit {edit_id} on {instance}: {status} {body}"
        self.acked[doc].add(edit_id)
        if acked_by is not None:
            acked_by.add(edit_id)
        if time.monotonic() - started > 1:
            log(f"edit {edit_id} on {instance} took {time.monotonic() - started:.1f} s")

    def close(self, instance: str, doc: str) -> float:
        status, body = self.instances[instance].request("POST", "/close", {"doc": doc}, timeout=5)
        assert status in (200, 202), f"POST /close {doc} on {instance}: {status} {body}"
        return time.monotonic()

    def status(self, instance: str, doc: str) -> dict | None:
        with contextlib.suppress(Exception):
            status, body = self.instances[instance].request("GET", f"/docs/{doc}", timeout=2)
            if status == 200:
                return body
        return None

    def wait_unloaded(self, instance: str, doc: str, closed_at: float) -> tuple[float, set[str]] | None:
        """Poll /docs until loaded:false; read the row at that instant. None if the deadline passes."""
        while time.monotonic() - closed_at < UNLOAD_DEADLINE_S:
            body = self.status(instance, doc)
            if body is not None and body.get("loaded") is False:
                _, edits = self.row(doc)
                return time.monotonic() - closed_at, edits
            time.sleep(UNLOAD_POLL_S)
        return None

    def lock_key(self, doc: str) -> str:
        return f"{self.prefix}:{doc}:lock"


class Background:
    """Run a function in a thread; keep its exception for the test to report."""

    def __init__(self, target, *args) -> None:
        self.error: BaseException | None = None
        self.result = None

        def run():
            try:
                self.result = target(*args)
            except BaseException as error:  # noqa: BLE001
                self.error = error
                log(f"background {getattr(target, '__name__', target)} failed: {error!r}")

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self, timeout: float = 60) -> None:
        self.thread.join(timeout)
