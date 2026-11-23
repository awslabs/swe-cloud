"""Verifier harness: provisions its own engines, plays the storefront app and the catalog export API,
runs the solution's sync-server and reindex CLI, and records what search served.

Nothing here imports /app. The solution is driven only through its command lines and the
/on_commit contract.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config

LOGS = Path(os.environ.get("VERIFIER_LOGS", "/logs/verifier"))
APP = Path(os.environ.get("APP_DIR", "/app"))

N_ROWS = 10000
PAGE_LIMIT_MAX = 250
HOLD_S = 75.0                 # past the default index.gc_deletes (60 s)
POINT_READ_INJECT_EVERY = 5
BACKGROUND_OPS_PER_S = 20.0
BACKGROUND_THREADS = 6
CLOCK_SKEW_S = 3.0            # writer W2's clock runs behind W1's
WARMUP_S = 10.0
HOOK_TIMEOUT_S = 2.0
LIVE_LAG_S = 2.0
PROBE_POLL_S = 0.05
SWAP_POLL_S = 0.02
SWAP_BURST_OPS = 20
SWEEP_AFTER_SWAP_S = 2.0
POST_EXIT_WRITE_S = 5.0
SETTLE_S = 3.0
REINDEX_DEADLINE_S = 600.0
BUDGET_FACTOR = 4
FREEZE_AFTER_PAGE_S = 0.03    # the ops node is drained just after the CLI received a page: its container is paused
RERUN_AFTER_S = 8.0           # ops notice and start the same command on another node; the paused one stays paused
SLOW_HOOK_ALLOWANCE = 0.005   # share of hook calls that may miss 2 s (venue noise: an engine connect can stall ~2 s)
REREAD_SLACK = 0.25           # a rerun may re-read part of the catalog, not all of what was copied
ALIAS = "products"
OLD_INDEX = "products_v1"
NOUNS = ["Lamp", "Desk", "Chair", "Kettle", "Mug", "Backpack", "Headphones", "Notebook", "Blender", "Rug"]
BRANDS = ["acme", "globex", "initech", "umbrella", "hooli", "stark", "wayne", "wonka"]


class EnvironmentFailure(RuntimeError):
    """The emulator or an engine never became usable: not a verdict on the solution."""


def log(msg: str) -> None:
    print(f"[harness {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def v2(row: dict) -> dict:
    return {"name": row["name"], "price_cents": int(row["price_cents"]), "brand": row["brand"]}


# ── engines ───────────────────────────────────────────────────────────────────────


@dataclass
class Engines:
    pg_host: str
    pg_port: int
    master_password: str
    indexer_password: str
    os_url: str

    @property
    def master_dsn(self) -> str:
        return f"host={self.pg_host} port={self.pg_port} dbname=postgres user=master password={self.master_password} connect_timeout=10"

    @property
    def indexer_dsn(self) -> str:
        return f"host={self.pg_host} port={self.pg_port} dbname=postgres user=catalog_indexer password={self.indexer_password} connect_timeout=10"

    @classmethod
    def provision(cls) -> "Engines":
        cfg = Config(read_timeout=600, connect_timeout=10, retries={"max_attempts": 1})
        suffix = secrets.token_hex(4)
        master_pw, indexer_pw = secrets.token_hex(12), secrets.token_hex(12)
        found: dict = {}
        errors: list = []

        def rds() -> None:
            try:
                client = boto3.client("rds", config=cfg)
                name = f"verify-catalog-{suffix}"
                client.create_db_instance(DBInstanceIdentifier=name, Engine="postgres", EngineVersion="16.3",
                                          DBInstanceClass="db.t3.micro", AllocatedStorage=20,
                                          MasterUsername="master", MasterUserPassword=master_pw)
                deadline = time.time() + 400
                while time.time() < deadline:
                    db = client.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
                    if db["DBInstanceStatus"] == "available" and db.get("Endpoint"):
                        found["pg"] = db["Endpoint"]
                        return
                    time.sleep(1)
                errors.append("RDS instance never became available")
            except Exception as error:  # noqa: BLE001
                errors.append(f"RDS: {type(error).__name__}: {error}")

        def opensearch() -> None:
            try:
                client = boto3.client("opensearch", config=cfg)
                name = f"verify-search-{suffix}"
                client.create_domain(DomainName=name, EngineVersion="OpenSearch_2.11")
                deadline = time.time() + 500
                while time.time() < deadline:
                    domain = client.describe_domain(DomainName=name)["DomainStatus"]
                    if domain.get("Endpoint") and not domain.get("Processing"):
                        endpoint = domain["Endpoint"]
                        found["os"] = endpoint if endpoint.startswith("http") else f"http://{endpoint}"
                        return
                    time.sleep(1)
                errors.append("OpenSearch domain never became available")
            except Exception as error:  # noqa: BLE001
                errors.append(f"OpenSearch: {type(error).__name__}: {error}")

        threads = [threading.Thread(target=f) for f in (rds, opensearch)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise EnvironmentFailure("; ".join(errors))
        engines = cls(found["pg"]["Address"], int(found["pg"]["Port"]), master_pw, indexer_pw, found["os"])
        engines._wait_ready()
        engines._setup_roles()
        log(f"engines: postgres {engines.pg_host}:{engines.pg_port}, opensearch {engines.os_url}")
        return engines

    def _wait_ready(self) -> None:
        deadline = time.time() + 120
        last = None
        while time.time() < deadline:
            try:
                with psycopg.connect(self.master_dsn, autocommit=True) as conn:
                    conn.execute("SELECT 1")
                status, _ = os_request(self.os_url, "GET", "/_cluster/health")
                if status == 200:
                    return
            except Exception as error:  # noqa: BLE001
                last = error
            time.sleep(1)
        raise EnvironmentFailure(f"engines not reachable: {last}")

    def _setup_roles(self) -> None:
        with psycopg.connect(self.master_dsn, autocommit=True) as conn:
            conn.execute(f"CREATE ROLE catalog_indexer LOGIN PASSWORD '{self.indexer_password}'")
            conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            conn.execute("CREATE SCHEMA harness")
            conn.execute("REVOKE ALL ON SCHEMA harness FROM PUBLIC")


def os_request(base: str, method: str, path: str, body: object = None, ndjson: str | None = None,
               timeout: float = 30.0) -> tuple[int, dict]:
    data, ctype = None, "application/json"
    if ndjson is not None:
        data, ctype = ndjson.encode(), "application/x-ndjson"
    elif body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except ValueError:
            return error.code, {"raw": raw[:500].decode(errors="replace")}


# ── one scenario ──────────────────────────────────────────────────────────────────


@dataclass(repr=False)
class Op:
    seq: int
    kind: str          # insert | update | delete
    pid: int
    role: str          # background, page-skew-w1, page-skew-w2, page-update, page-delete, held-delete, page-insert, tail-insert, point-update, point-delete, swap-burst
    writer: str
    after_t0: bool
    expect: dict | None  # v2 projection, or None for a delete
    committed_at: float = 0.0
    hook_status: int | str = 0
    hook_ms: float = 0.0
    probe: str = ""      # "" ok, else a description of the miss


@dataclass(repr=False)
class Result:
    seed: int
    error: str = ""
    ops: list = field(default_factory=list)
    writer_errors: list = field(default_factory=list)
    sweep_misses: list = field(default_factory=list)
    swap_seen_at: float | None = None
    sweep_checked: int = 0
    cli_rc: int | None = None
    cli_seconds: float = 0.0
    cli_timed_out: bool = False
    cli_tail: str = ""
    server_died: str = ""
    t0: float = 0.0
    rows_at_t0: int = 0
    alias_indices: list = field(default_factory=list)
    new_meta: object = None
    new_created_ms: int = 0
    table: dict = field(default_factory=dict)
    served: dict = field(default_factory=dict)
    deleted: set = field(default_factory=set)
    alias_count: int | None = None
    indexing: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    pages_served: int = 0
    rows_served: int = 0
    interrupted_after_page: int | None = None
    first_cli_rc: int | None = None
    first_cli_tail: str = ""

    @property
    def inserts_after_t0(self) -> int:
        return sum(1 for op in self.ops if op.after_t0 and op.kind == "insert")

    @property
    def mutations_after_t0(self) -> int:
        return sum(1 for op in self.ops if op.after_t0)

    def role_of(self, pid: int) -> str:
        return ",".join(sorted(self.roles.get(pid, {"background"})))


class Scenario:
    def __init__(self, engines: Engines, seed: int, held_ordinal: int, kill_ordinal: int):
        self.e = engines
        self.seed = seed
        self.held_ordinal = held_ordinal
        self.kill_ordinal = kill_ordinal
        self.cli: subprocess.Popen | None = None
        self.interrupted = threading.Event()
        self.frozen: subprocess.Popen | None = None
        self.rng = random.Random(seed)
        self.result = Result(seed=seed)
        self.state = threading.Lock()
        self.locks: dict[int, threading.Lock] = {}
        self.live: list[int] = []
        self.live_pos: dict[int, int] = {}
        self.touched: set[int] = set()
        self.seq = 0
        self.local = threading.local()
        self.injecting = False
        self.page_ordinal = 0
        self.point_reads = 0
        self.tail_inserted = False
        self.stop_background = threading.Event()
        self.stop_watch = threading.Event()
        self.hook_url = ""
        self.export_port = free_port()
        self.hook_port = free_port()
        self.workdir = Path(tempfile.mkdtemp(prefix=f"verify-{seed}-"))

    # db ----------------------------------------------------------------------

    def conn(self) -> psycopg.Connection:
        conn = getattr(self.local, "conn", None)
        if conn is None or conn.closed:
            conn = psycopg.connect(self.e.master_dsn, autocommit=True)
            conn.execute("SET statement_timeout = 2000")
            self.local.conn = conn
        return conn

    def lock_for(self, pid: int) -> threading.Lock:
        with self.state:
            lock = self.locks.get(pid)
            if lock is None:
                lock = self.locks[pid] = threading.Lock()
            return lock

    def _add_live(self, pid: int) -> None:
        with self.state:
            if pid not in self.live_pos:
                self.live_pos[pid] = len(self.live)
                self.live.append(pid)

    def _drop_live(self, pid: int) -> None:
        with self.state:
            pos = self.live_pos.pop(pid, None)
            if pos is None:
                return
            last = self.live.pop()
            if last != pid:
                self.live[pos] = last
                self.live_pos[last] = pos

    def _random_live(self, rng: random.Random) -> int | None:
        with self.state:
            return rng.choice(self.live) if self.live else None

    def clock(self, writer: str) -> dt.datetime:
        now = dt.datetime.now(dt.timezone.utc)
        return now - dt.timedelta(seconds=CLOCK_SKEW_S) if writer == "W2" else now

    def new_fields(self, rng: random.Random, pid: int | None = None) -> dict:
        tag = rng.randrange(10**6)
        return {
            "name": f"{rng.choice(NOUNS)} {pid if pid is not None else 'new'}-{tag}",
            "price_cents": rng.randrange(199, 99999),
            "brand": rng.choice(BRANDS),
        }

    # the app: commit, hook, probe -------------------------------------------------

    def change(self, kind: str, pid: int | None, writer: str, role: str, rng: random.Random) -> Op | None:
        """One app change. For update/delete the caller holds the product's lock."""
        try:
            if kind == "insert":
                # take the new product's lock before its row is visible, so no other change can overtake it
                f = self.new_fields(rng)
                pid = self.conn().execute("SELECT nextval('products_id_seq')").fetchone()[0]
                lock = self.lock_for(pid)
                lock.acquire()
                try:
                    found = self.conn().execute(
                        "INSERT INTO products (id, name, price_cents, brand, updated_at) VALUES (%s, %s, %s, %s, %s)"
                        " RETURNING id, name, price_cents, brand, updated_at",
                        (pid, f["name"], f["price_cents"], f["brand"], self.clock(writer))).fetchone()
                    return self._after_commit(kind, pid, writer, role, found)
                finally:
                    lock.release()
                    self._add_live(pid)
            if kind == "update":
                f = self.new_fields(rng, pid)
                if rng.random() < 0.7:
                    f.pop("brand")
                sets = ", ".join(f"{k} = %s" for k in f)
                found = self.conn().execute(
                    f"UPDATE products SET {sets}, updated_at = %s WHERE id = %s RETURNING id, name, price_cents, brand, updated_at",
                    (*f.values(), self.clock(writer), pid)).fetchone()
                if found is None:
                    return None
                return self._after_commit(kind, pid, writer, role, found)
            if kind == "delete":
                gone = self.conn().execute("DELETE FROM products WHERE id = %s RETURNING id", (pid,)).fetchone()
                if gone is None:
                    return None
                self._drop_live(pid)
                with self.state:
                    self.result.deleted.add(pid)
                return self._after_commit(kind, pid, writer, role, None)
        except psycopg.Error as error:
            with self.state:
                self.result.writer_errors.append(f"{kind} {pid} ({role}): {type(error).__name__}: {error}")
            self.local.conn = None
        return None

    def _after_commit(self, kind: str, pid: int, writer: str, role: str, found) -> Op:
        committed_at = time.time()
        row = None
        if found is not None:
            row = {"id": found[0], "name": found[1], "price_cents": found[2], "brand": found[3], "updated_at": found[4].isoformat()}
        with self.state:
            self.seq += 1
            op = Op(self.seq, kind, pid, role, writer, self.result.t0 > 0, v2(row) if row else None, committed_at)
            self.result.ops.append(op)
            if op.after_t0:
                self.touched.add(pid)
                if role != "background":
                    self.result.roles.setdefault(pid, set()).add(role)
        change = {"id": pid, "op": "delete" if kind == "delete" else "upsert", "row": row}
        request = urllib.request.Request(self.hook_url, data=json.dumps(change).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=HOOK_TIMEOUT_S) as resp:
                op.hook_status = resp.status
        except urllib.error.HTTPError as error:
            op.hook_status = error.code
        except Exception as error:  # noqa: BLE001 - timeout, refused, reset
            op.hook_status = type(error).__name__
        op.hook_ms = (time.monotonic() - start) * 1000
        op.probe = self.probe(pid, op.expect)
        return op

    def served_doc(self, pid: int) -> tuple[str, dict | None]:
        status, body = os_request(self.e.os_url, "GET", f"/{ALIAS}/_doc/{pid}", timeout=5)
        if status == 200 and body.get("found"):
            return "found", body.get("_source") or {}
        if status == 404 and body.get("found") is False:
            return "absent", None
        return f"error {status}", body

    @staticmethod
    def fresh(state: str, source: dict | None, expect: dict | None) -> bool:
        if expect is None:
            return state == "absent"
        return state == "found" and source.get("name") == expect["name"] and source.get("price_cents") == expect["price_cents"]

    def probe(self, pid: int, expect: dict | None) -> str:
        deadline = time.monotonic() + LIVE_LAG_S
        state, source = "unread", None
        while True:
            try:
                state, source = self.served_doc(pid)
            except Exception as error:  # noqa: BLE001
                state, source = f"error {type(error).__name__}", None
            if self.fresh(state, source, expect):
                return ""
            if time.monotonic() >= deadline:
                want = "absent" if expect is None else f"name={expect['name']!r} price_cents={expect['price_cents']}"
                got = state if source is None or state != "found" else f"name={source.get('name')!r} price_cents={source.get('price_cents')}"
                return f"wanted {want}, alias served {got}"
            time.sleep(PROBE_POLL_S)

    # background writers ------------------------------------------------------------

    def background(self, index: int) -> None:
        rng = random.Random(self.seed * 1000 + index)
        interval = BACKGROUND_THREADS / BACKGROUND_OPS_PER_S
        next_at = time.monotonic() + rng.random() * interval
        while not self.stop_background.is_set():
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_at = max(next_at + interval, time.monotonic())
            writer = "W2" if rng.random() < 0.5 else "W1"
            roll = rng.random()
            if roll < 0.2:
                self.change("insert", None, writer, "background", rng)
                continue
            kind = "update" if roll < 0.8 else "delete"
            for _ in range(10):
                pid = self._random_live(rng)
                if pid is None:
                    break
                lock = self.lock_for(pid)
                if lock.acquire(blocking=False):
                    try:
                        self.change(kind, pid, writer, "background", rng)
                    finally:
                        lock.release()
                    break

    # export API with the race windows placed in it ------------------------------------

    def inject_page(self, rows: list, ordinal: int, full: bool) -> None:
        rng = random.Random(self.seed * 7919 + ordinal)
        ids = [r[0] for r in rows]
        rng.shuffle(ids)
        plan = ["skew", "update", "held-delete" if ordinal == self.held_ordinal else "page-delete"]
        for pid in ids:
            if not plan:
                break
            lock = self.lock_for(pid)
            if not lock.acquire(blocking=False):
                continue
            try:
                step = plan.pop(0)
                if step == "skew":
                    self.change("update", pid, "W1", "page-skew-w1", rng)
                    self.change("update", pid, "W2", "page-skew-w2", rng)
                elif step == "update":
                    self.change("update", pid, "W1", "page-update", rng)
                else:
                    self.change("delete", pid, "W1", step, rng)
            finally:
                lock.release()
        # A new product lands past the cursor. Only full pages and the first empty page get one, so
        # paging still reaches the end of the table.
        if full or (not rows and not self.tail_inserted):
            self.tail_inserted = self.tail_inserted or not rows
            self.change("insert", None, "W2" if ordinal % 2 else "W1", "page-insert" if rows else "tail-insert", rng)

    def inject_point(self, pid: int, k: int) -> None:
        lock = self.lock_for(pid)
        if not lock.acquire(blocking=False):
            return
        try:
            rng = random.Random(self.seed * 104729 + k)
            if (k // POINT_READ_INJECT_EVERY) % 2:
                self.change("delete", pid, "W1", "point-delete", rng)
            else:
                self.change("update", pid, "W2", "point-update", rng)
        finally:
            lock.release()

    def export_handler(self):
        scenario = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query)
                try:
                    if url.path == "/rows":
                        after_id = int(query.get("after_id", ["0"])[0])
                        limit = int(query.get("limit", [str(PAGE_LIMIT_MAX)])[0])
                        if not 0 < limit <= PAGE_LIMIT_MAX:
                            self._reply(400, {"error": f"limit must be 1..{PAGE_LIMIT_MAX}"})
                            return
                        with psycopg.connect(scenario.e.master_dsn, autocommit=True) as conn:
                            rows = conn.execute("SELECT id, name, price_cents, brand, updated_at FROM products"
                                                " WHERE id > %s ORDER BY id LIMIT %s", (after_id, limit)).fetchall()
                        ordinal = None
                        if scenario.injecting:
                            with scenario.state:
                                scenario.page_ordinal += 1
                                ordinal = scenario.page_ordinal
                                scenario.result.rows_served += len(rows)
                            scenario.inject_page(rows, ordinal, full=len(rows) == limit and limit >= 50)
                            if ordinal == scenario.held_ordinal:
                                time.sleep(HOLD_S)
                        self._reply(200, {"rows": [serialize(r) for r in rows]})
                        if ordinal == scenario.kill_ordinal:
                            threading.Timer(FREEZE_AFTER_PAGE_S, scenario.interrupt_cli, args=(ordinal,)).start()
                    elif url.path.startswith("/row/"):
                        pid = int(url.path.rsplit("/", 1)[1])
                        with psycopg.connect(scenario.e.master_dsn, autocommit=True) as conn:
                            found = conn.execute("SELECT id, name, price_cents, brand, updated_at FROM products WHERE id = %s",
                                                 (pid,)).fetchone()
                        if found is not None and scenario.injecting:
                            with scenario.state:
                                scenario.point_reads += 1
                                k = scenario.point_reads
                            if k % POINT_READ_INJECT_EVERY == 0:
                                scenario.inject_point(pid, k)
                        if found is None:
                            self._reply(404, {"error": "no such product"})
                        else:
                            self._reply(200, serialize(found))
                    else:
                        self._reply(404, {"error": "not found"})
                except ValueError as error:
                    self._reply(400, {"error": str(error)})

            def log_message(self, fmt, *args) -> None:
                pass

        return Handler

    # alias watcher: burst on the swap, then sweep -------------------------------------

    def watch_alias(self) -> None:
        while not self.stop_watch.is_set():
            try:
                status, body = os_request(self.e.os_url, "GET", f"/_alias/{ALIAS}", timeout=5)
            except Exception:  # noqa: BLE001
                status, body = 0, {}
            if status == 200 and sorted(body) != [OLD_INDEX]:
                self.result.swap_seen_at = time.time()
                log(f"seed {self.seed}: alias moved to {sorted(body)}")
                self.swap_burst()
                time.sleep(SWEEP_AFTER_SWAP_S)
                self.sweep()
                return
            time.sleep(SWAP_POLL_S)

    def swap_burst(self) -> None:
        rng = random.Random(self.seed * 31337)
        with self.state:
            candidates = sorted(p for p in self.touched if p in self.live_pos)
        rng.shuffle(candidates)

        def one(pid: int, kind: str) -> None:
            lock = self.lock_for(pid)
            if lock.acquire(timeout=3):
                try:
                    self.change(kind, pid, "W1", "swap-burst", random.Random(pid))
                finally:
                    lock.release()

        threads = [threading.Thread(target=one, args=(pid, "delete" if i % 5 == 4 else "update"))
                   for i, pid in enumerate(candidates[:SWAP_BURST_OPS])]
        for t in threads:
            t.start()
        self._burst_threads = threads

    def sweep(self) -> None:
        with self.state:
            ids = sorted(self.touched)
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            held = []
            try:
                for pid in batch:
                    lock = self.lock_for(pid)
                    if lock.acquire(timeout=10):
                        held.append(pid)
                rows = {r[0]: {"name": r[1], "price_cents": r[2]} for r in self.conn().execute(
                    "SELECT id, name, price_cents FROM products WHERE id = ANY(%s)", (held,)).fetchall()}
                status, body = os_request(self.e.os_url, "POST", f"/{ALIAS}/_mget", {"ids": [str(p) for p in held]}, timeout=15)
                docs = {int(d["_id"]): d for d in body.get("docs", [])} if status == 200 else {}
                for pid in held:
                    d = docs.get(pid)
                    state = "found" if d and d.get("found") else ("absent" if d is not None else f"error {status}")
                    expect = rows.get(pid)
                    if not self.fresh(state, (d or {}).get("_source"), expect):
                        self.result.sweep_misses.append(
                            f"product {pid} ({self.result.role_of(pid)}): catalog "
                            f"{'deleted' if expect is None else expect}, alias served {state} {(d or {}).get('_source')}")
                self.result.sweep_checked += len(held)
            except Exception as error:  # noqa: BLE001
                self.result.sweep_misses.append(f"sweep failed: {type(error).__name__}: {error}")
            finally:
                for pid in held:
                    self.lock_for(pid).release()

    def interrupt_cli(self, ordinal: int) -> None:
        """The ops node running bin/reindex is drained: the job's container is paused at once, as a cgroup freeze does.

        The process group never runs again, but its sockets stay open (the kernel still answers for them), so
        Postgres keeps its sessions and whatever they hold until the group is killed after the rerun is done.
        """
        cli = self.cli
        if cli is None or cli.poll() is not None or self.interrupted.is_set():
            return
        try:
            os.killpg(cli.pid, signal.SIGSTOP)
        except ProcessLookupError:
            return
        self.frozen = cli
        self.result.interrupted_after_page = ordinal
        self.interrupted.set()

    def remove_frozen(self) -> None:
        """The paused container is finally removed."""
        if self.frozen is not None:
            try:
                os.killpg(self.frozen.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.frozen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    # world reset and processes ---------------------------------------------------

    def reset_world(self) -> None:
        rng = random.Random(self.seed)
        with psycopg.connect(self.e.master_dsn, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'catalog_indexer'")
            conn.execute("DROP TABLE IF EXISTS products")
            conn.execute("DROP SCHEMA IF EXISTS reindex_aux CASCADE")
            conn.execute("CREATE SCHEMA reindex_aux AUTHORIZATION catalog_indexer")
            conn.execute("CREATE TABLE products (id bigserial PRIMARY KEY, name text NOT NULL, price_cents integer NOT NULL,"
                         " brand text NOT NULL, updated_at timestamptz NOT NULL)")
            conn.execute("REVOKE ALL ON products FROM PUBLIC")
            seeded = [(f"{rng.choice(NOUNS)} {i}", rng.randrange(199, 99999), rng.choice(BRANDS)) for i in range(1, N_ROWS + 1)]
            stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            with conn.cursor() as cur, cur.copy("COPY products (name, price_cents, brand, updated_at) FROM STDIN") as copy:
                for name, price, brand in seeded:
                    copy.write_row((name, price, brand, stamp))
            ids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY id").fetchall()]
        status, indices = os_request(self.e.os_url, "GET", "/_cat/indices?format=json")
        for entry in indices if status == 200 and isinstance(indices, list) else []:
            if not entry["index"].startswith("."):
                os_request(self.e.os_url, "DELETE", f"/{entry['index']}")
        status, body = os_request(self.e.os_url, "PUT", f"/{OLD_INDEX}", {
            "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
            "mappings": {"_meta": {"search_schema": 1}, "dynamic": False,
                         "properties": {"name": {"type": "text"}, "price_cents": {"type": "integer"}}}})
        if status != 200:
            raise EnvironmentFailure(f"cannot create {OLD_INDEX}: {status} {body}")
        for start in range(0, len(seeded), 2000):
            lines = []
            for offset, (name, price, _brand) in enumerate(seeded[start:start + 2000]):
                lines.append(json.dumps({"index": {"_index": OLD_INDEX, "_id": str(ids[start + offset])}}))
                lines.append(json.dumps({"name": name, "price_cents": price}))
            status, body = os_request(self.e.os_url, "POST", "/_bulk", ndjson="\n".join(lines) + "\n", timeout=120)
            if status != 200 or body.get("errors"):
                raise EnvironmentFailure(f"cannot load {OLD_INDEX}: {status}")
        os_request(self.e.os_url, "POST", "/_aliases", {"actions": [{"add": {"index": OLD_INDEX, "alias": ALIAS}}]})
        os_request(self.e.os_url, "POST", f"/{OLD_INDEX}/_refresh")
        for pid in ids:
            self._add_live(pid)

    def solution_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "VERIFIER"))}
        env.update({
            "OPENSEARCH_URL": self.e.os_url,
            "INDEXER_DSN": self.e.indexer_dsn,
            "CATALOG_EXPORT_URL": f"http://127.0.0.1:{self.export_port}",
            "PYTHONPATH": str(APP),
            "PYTHONUNBUFFERED": "1",
        })
        env.pop("CATALOG_DSN", None)
        return env

    def start_cli(self, log_name: str) -> subprocess.Popen:
        cwd = tempfile.mkdtemp(prefix="reindex-")
        out = open(self.workdir / log_name, "wb")
        return subprocess.Popen([str(APP / "bin" / "reindex"), "--alias", ALIAS, "--schema-version", "2"], cwd=cwd,
                                env={**self.solution_env(), "TMPDIR": cwd}, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)

    def wait_cli(self, cli: subprocess.Popen, deadline: float) -> None:
        """Until the CLI exits, is frozen by interrupt_cli, or runs out of time."""
        while True:
            try:
                self.result.cli_rc = cli.wait(timeout=0.2)
                return
            except subprocess.TimeoutExpired:
                pass
            if self.frozen is cli:
                return
            if time.time() >= deadline:
                self.result.cli_timed_out = True
                kill_group(cli)
                return

    def start_server(self) -> subprocess.Popen:
        cwd = tempfile.mkdtemp(prefix="sync-server-")
        out = open(self.workdir / "sync-server.log", "wb")
        proc = subprocess.Popen([str(APP / "bin" / "sync-server"), "--port", str(self.hook_port)], cwd=cwd,
                                env={**self.solution_env(), "TMPDIR": cwd}, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"sync-server exited with {proc.returncode} on start: {tail(self.workdir / 'sync-server.log')}")
            try:
                with socket.create_connection(("127.0.0.1", self.hook_port), timeout=1):
                    return proc
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("sync-server did not listen within 60 s")

    # the run ------------------------------------------------------------------

    def run(self) -> Result:
        export = ThreadingHTTPServer(("127.0.0.1", self.export_port), self.export_handler())
        export.daemon_threads = True
        threading.Thread(target=export.serve_forever, daemon=True).start()
        self.hook_url = f"http://127.0.0.1:{self.hook_port}/on_commit"
        server = None
        writers: list[threading.Thread] = []
        try:
            self.reset_world()
            server = self.start_server()
            log(f"seed {self.seed}: world reset, sync-server up, warm-up {WARMUP_S:.0f} s")
            writers = [threading.Thread(target=self.background, args=(i,), daemon=True) for i in range(BACKGROUND_THREADS)]
            for t in writers:
                t.start()
            time.sleep(WARMUP_S)

            with psycopg.connect(self.e.master_dsn, autocommit=True) as conn:
                self.result.rows_at_t0 = conn.execute("SELECT count(*) FROM products").fetchone()[0]
            self.result.t0 = time.time()
            self.injecting = True
            watcher = threading.Thread(target=self.watch_alias, daemon=True)
            watcher.start()
            deadline = self.result.t0 + REINDEX_DEADLINE_S
            cli = self.cli = self.start_cli("reindex.log")
            log(f"seed {self.seed}: reindex started (held page #{self.held_ordinal}, node drained after page #{self.kill_ordinal})")
            self.wait_cli(cli, deadline)
            if self.interrupted.is_set() and not self.result.cli_timed_out:
                self.result.first_cli_rc = self.result.cli_rc
                self.result.first_cli_tail = tail(self.workdir / "reindex.log")
                log(f"seed {self.seed}: reindex paused (node drained) after page #{self.result.interrupted_after_page}; "
                    f"rerun in {RERUN_AFTER_S:.0f} s")
                time.sleep(RERUN_AFTER_S)
                self.result.cli_rc = None
                cli = self.cli = self.start_cli("reindex-rerun.log")
                self.wait_cli(cli, deadline)
                self.result.cli_tail = tail(self.workdir / "reindex-rerun.log")
                self.remove_frozen()
            else:
                self.result.cli_tail = tail(self.workdir / "reindex.log")
            self.result.cli_seconds = time.time() - self.result.t0
            log(f"seed {self.seed}: reindex exited rc={self.result.cli_rc} after {self.result.cli_seconds:.1f} s")
            self.injecting = False
            self.result.pages_served = self.page_ordinal

            time.sleep(POST_EXIT_WRITE_S)
            self.stop_background.set()
            for t in writers:
                t.join(timeout=30)
            if self.result.swap_seen_at is None:
                self.stop_watch.set()
            watcher.join(timeout=120)
            self.stop_watch.set()
            for t in getattr(self, "_burst_threads", []):
                t.join(timeout=10)
            if server.poll() is not None:
                self.result.server_died = f"sync-server exited with {server.returncode}: {tail(self.workdir / 'sync-server.log')}"
            time.sleep(SETTLE_S)
            self.observe()
        except EnvironmentFailure:
            raise
        except Exception as error:  # noqa: BLE001
            self.result.error = f"{type(error).__name__}: {error}"
            log(f"seed {self.seed}: scenario error {self.result.error}")
        finally:
            self.stop_background.set()
            self.stop_watch.set()
            for t in writers:
                t.join(timeout=10)
            self.remove_frozen()
            if server is not None:
                kill_group(server)
            export.shutdown()
            export.server_close()
            self.dump()
        return self.result

    def observe(self) -> None:
        r = self.result
        status, body = os_request(self.e.os_url, "GET", f"/_alias/{ALIAS}")
        r.alias_indices = sorted(body) if status == 200 else []
        with psycopg.connect(self.e.master_dsn, autocommit=True) as conn:
            for found in conn.execute("SELECT id, name, price_cents, brand FROM products").fetchall():
                r.table[found[0]] = {"name": found[1], "price_cents": found[2], "brand": found[3]}
        if len(r.alias_indices) == 1:
            index = r.alias_indices[0]
            _, mapping = os_request(self.e.os_url, "GET", f"/{index}/_mapping")
            r.new_meta = (mapping.get(index, {}).get("mappings", {}).get("_meta") or {}).get("search_schema")
            _, settings = os_request(self.e.os_url, "GET", f"/{index}/_settings")
            r.new_created_ms = int(settings.get(index, {}).get("settings", {}).get("index", {}).get("creation_date", 0))
            os_request(self.e.os_url, "POST", f"/{ALIAS}/_refresh")
            status, count = os_request(self.e.os_url, "GET", f"/{ALIAS}/_count")
            r.alias_count = count.get("count") if status == 200 else None
            ids = sorted(set(r.table) | r.deleted)
            for start in range(0, len(ids), 1000):
                status, got = os_request(self.e.os_url, "POST", f"/{ALIAS}/_mget",
                                         {"ids": [str(p) for p in ids[start:start + 1000]]}, timeout=60)
                for d in got.get("docs", []) if status == 200 else []:
                    if d.get("found"):
                        r.served[int(d["_id"])] = d.get("_source")
            _, stats = os_request(self.e.os_url, "GET", f"/{index}/_stats/indexing")
            r.indexing = stats.get("_all", {}).get("primaries", {}).get("indexing", {})

    def dump(self) -> None:
        r = self.result
        LOGS.mkdir(parents=True, exist_ok=True)
        summary = {
            "seed": r.seed, "held_ordinal": self.held_ordinal, "kill_ordinal": self.kill_ordinal,
            "interrupted_after_page": r.interrupted_after_page, "first_cli_rc": r.first_cli_rc,
            "rows_served": r.rows_served, "inserts_after_t0": r.inserts_after_t0, "error": r.error, "cli_rc": r.cli_rc,
            "cli_seconds": round(r.cli_seconds, 1), "cli_timed_out": r.cli_timed_out, "pages_served": r.pages_served,
            "ops": len(r.ops), "mutations_after_t0": r.mutations_after_t0, "rows_at_t0": r.rows_at_t0,
            "hook_failures": [f"{o.kind} {o.pid} ({o.role}): {o.hook_status} in {o.hook_ms:.0f} ms" for o in r.ops
                              if not (isinstance(o.hook_status, int) and 200 <= o.hook_status < 300) or o.hook_ms > HOOK_TIMEOUT_S * 1000][:50],
            "probe_misses": [f"{o.kind} {o.pid} ({o.role}): {o.probe}" for o in r.ops if o.probe][:50],
            "sweep_checked": r.sweep_checked, "sweep_misses": r.sweep_misses[:50], "writer_errors": r.writer_errors[:20],
            "alias_indices": r.alias_indices, "new_meta": r.new_meta, "indexing": r.indexing,
            "alias_count": r.alias_count, "table_rows": len(r.table), "deleted": len(r.deleted),
            "server_died": r.server_died, "cli_tail": r.cli_tail[-3000:],
        }
        (LOGS / f"seed-{r.seed}.json").write_text(json.dumps(summary, indent=2, default=str))
        for name in ("reindex.log", "reindex-rerun.log", "sync-server.log"):
            path = self.workdir / name
            if path.exists():
                (LOGS / f"seed-{r.seed}-{name}").write_bytes(path.read_bytes()[-200_000:])


def serialize(found) -> dict:
    return {"id": found[0], "name": found[1], "price_cents": found[2], "brand": found[3], "updated_at": found[4].isoformat()}


def tail(path: Path, size: int = 4000) -> str:
    try:
        return path.read_bytes()[-size:].decode(errors="replace")
    except OSError:
        return ""


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except PermissionError:
        proc.kill()
