"""Verifier harness for stepq: one scenario against a fresh ElastiCache Valkey cluster.

Roles, all in this process except the executors:
  proxy      one listener per executor between it and Valkey; injects per-item faults
  runner     the step runner (HTTP), behind a load balancer that answers 504 to a request idle for LB_IDLE_MS
             and drops it (README); runs are keyed by (item_id, lease_id), a repeat POST attaches to its run
  sampler    privileged reads of every item's stored lease and Valkey TIME every SAMPLE_MS
  batch      the batch fleet: leases/extends/dequeues W itself; scavenges Z with a stale read
  deploy     restarts the executor whose lease of S just committed: SIGTERM, wait for it to exit, start
             it again under the same id (README: deploys restart executors at any moment)
  host death kills Z's holder as its lease commits, and D's holder the moment D's step has finished
  executors  `python -m stepq.executor --id E<n>`, each in its own empty cwd, PYTHONPATH=/app, on a host
             clock that runs a few seconds behind the shard's (hostclock.so; README "Host clocks")
Nothing here imports /app.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
import redis
from botocore.config import Config

LOG_DIR = Path(os.environ.get("STEPQ_VERIFY_LOGS", "/logs/verifier"))

EXECUTORS = 3
WORKER_SLOTS = 2
POLL_MS = 100
SAMPLE_MS = 100
REPLY_WITHHOLD_MS = 1100
SLOW_CALL_MS = 4000
BATCH_LEASE_MS = 8000
BATCH_EXTEND_MS = 2000
BATCH_HOLD_MS = 8000
FOREIGN_SCAVENGE_AFTER_MS = 300
# A deploy gives a stopped executor this long to exit before it is killed, then starts it again.
GRACEFUL_EXIT_S = 30
# The load balancer in front of the step runner gives up on a request with no answer after this long (README).
LB_IDLE_MS = 8000
# Executor hosts keep their own time: each executor's clock runs this far behind the shard's (README).
HOST_CLOCK_LAG_MS = (3000, 3800)
HOST_CLOCK_LIB = Path(__file__).with_name("hostclock.so")  # built from hostclock.c
STEP_MS = {"X": 500, "Y": 17000, "Z": 2000, "N": 300, "W": 300, "S": 500, "F": 300, "D": 7000}

ITEMS = "{q}:queue:item"
CONCURRENCY = "{q}:concurrency"
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
READS = {
    "GET", "MGET", "HGET", "HMGET", "HGETALL", "HEXISTS", "HKEYS", "HVALS", "HLEN", "HSTRLEN", "HSCAN",
    "ZRANGE", "ZRANGEBYSCORE", "ZREVRANGE", "ZREVRANGEBYSCORE", "ZSCORE", "ZMSCORE", "ZCARD", "ZCOUNT",
    "ZRANK", "ZREVRANK", "ZSCAN", "ZRANDMEMBER", "EXISTS", "TYPE", "TTL", "PTTL", "SCAN", "KEYS", "TIME",
    "PING", "ECHO", "SMEMBERS", "SISMEMBER", "SCARD", "LRANGE", "LLEN", "STRLEN", "INFO", "DBSIZE",
    "FCALL_RO", "EVAL_RO", "EVALSHA_RO", "SELECT", "HELLO", "CLIENT", "FUNCTION", "COMMAND", "READONLY",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def ulid_new(ts_ms: int) -> str:
    value = (int(ts_ms) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid_time(value: str) -> int | None:
    try:
        ts = 0
        for c in value[:10].upper():
            ts = ts * 32 + _INDEX[c]
        return ts
    except (KeyError, TypeError):
        return None


def looks_like_ulid(value: str) -> bool:
    return len(value) == 26 and all(c in _INDEX for c in value.upper())


# ── RESP framing ──────────────────────────────────────────────────────────────────


class Closed(Exception):
    pass


class Reader:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise Closed()
        self.buf += chunk

    def line(self) -> bytes:
        while True:
            i = self.buf.find(b"\r\n")
            if i >= 0:
                out = bytes(self.buf[: i + 2])
                del self.buf[: i + 2]
                return out
            self._fill()

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def command(self) -> tuple[list[str], bytes]:
        first = self.line()
        if not first.startswith(b"*"):
            text = first.decode(errors="replace").strip()
            return (text.split() if text else []), first
        raw = [first]
        args = []
        for _ in range(int(first[1:-2])):
            head = self.line()
            raw.append(head)
            n = int(head[1:-2])
            body = self.exact(n + 2)
            raw.append(body)
            args.append(body[:-2].decode(errors="replace"))
        return args, b"".join(raw)

    def reply(self) -> bytes:
        head = self.line()
        kind = head[:1]
        if kind in (b"+", b"-", b":", b"_", b",", b"#", b"("):
            return head
        if kind in (b"$", b"=", b"!"):
            n = int(head[1:-2])
            return head if n < 0 else head + self.exact(n + 2)
        if kind in (b"*", b"~", b">", b"%", b"|"):
            n = int(head[1:-2])
            if n < 0:
                return head
            count = n * 2 if kind in (b"%", b"|") else n
            parts = [head] + [self.reply() for _ in range(count)]
            if kind == b"|":
                parts.append(self.reply())
            return b"".join(parts)
        raise Closed()


def encode_command(args: list[str]) -> bytes:
    out = [f"*{len(args)}\r\n".encode()]
    for a in args:
        b = a.encode()
        out.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
    return b"".join(out)


# ── scenario state ────────────────────────────────────────────────────────────────


@dataclass
class Tracked:
    label: str
    kind: str
    item_id: str
    partition: str
    step_ms: int
    enqueued_ms: int | None = None
    lease_ids: set = field(default_factory=set)
    lease_owner: dict = field(default_factory=dict)  # lease ID -> executor (or "batch") that first sent it
    lease_seen: dict = field(default_factory=dict)  # lease ID -> local ms it was first sent
    fired: bool = False
    fired_ms: int | None = None
    fired_by: str | None = None
    committed_lease: str | None = None
    slow: bool = False
    removed_ms: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Run:
    item_id: str
    label: str
    executor: str
    lease_id: str | None
    start_ms: int
    stored_lease: str | None
    end_ms: int | None = None
    stored_owner: str | None = None
    ok: bool = True
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    killed: str | None = None  # the executor killed as this run ended (D), whose answer is never sent

    def facts(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "done"}


class Scenario:
    def __init__(self):
        self.events: list[dict] = []
        self.errors: list[str] = []
        self.items: dict[str, Tracked] = {}
        self.runs: list[Run] = []  # runs the step runner started (one per new (item_id, lease_id))
        self.posts: list[dict] = []  # every POST /run: pair, executor, new run or attach, answer
        self.batch_runs: list[Run] = []
        self.batch_calls: list[dict] = []
        self.foreign_scavenge: dict | None = None
        self.samples: list[tuple[int, int, dict]] = []
        self.bypass: list[str] = []
        self.ready: dict[str, bool] = {}
        self.functions: list[str] = []
        self.killed: str | None = None
        self.died_after_step: dict | None = None
        self.restarted: dict | None = None
        self.envs: dict[str, dict] = {}
        self.clock_lag: dict[str, int] = {}
        self.lock = threading.RLock()
        self.procs: dict[str, subprocess.Popen] = {}
        self.port_owner: dict[int, str] = {}
        self.sampling = threading.Event()
        self.t0: int | None = None
        self.phases: dict[str, int] = {}

    def event(self, kind: str, **facts) -> None:
        with self.lock:
            self.events.append({"t": now_ms(), "event": kind, **facts})

    def tracked_by_label(self, label: str) -> Tracked | None:
        for t in self.items.values():
            if t.label == label:
                return t
        return None

    def named(self, args: list[str]) -> list[Tracked]:
        out = []
        for t in self.items.values():
            for a in args[1:]:
                if t.item_id in a or any(l in a for l in t.lease_ids):
                    out.append(t)
                    break
        return out


# ── proxy ─────────────────────────────────────────────────────────────────────────


class Proxy:
    def __init__(self, sc: Scenario, upstream: tuple[str, int], priv_factory):
        self.sc = sc
        self.upstream = upstream
        self.priv = priv_factory()
        self.batch = priv_factory()
        self.listeners: dict[str, socket.socket] = {}
        self.ports: dict[str, int] = {}

    def listen(self, executor_id: str) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(64)
        port = s.getsockname()[1]
        self.listeners[executor_id] = s
        self.ports[executor_id] = port
        threading.Thread(target=self._accept, args=(executor_id, s), daemon=True).start()
        return port

    def _accept(self, executor_id: str, s: socket.socket) -> None:
        while True:
            try:
                client, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(executor_id, client), daemon=True).start()

    def _open_upstream(self) -> tuple[socket.socket, Reader]:
        up = socket.create_connection(self.upstream, timeout=10)
        up.settimeout(None)
        reader = Reader(up)
        up.sendall(encode_command(["CLIENT", "SETNAME", "stepq-proxy"]))
        reader.reply()
        return up, reader

    def _serve(self, executor_id: str, client: socket.socket) -> None:
        up = None
        try:
            up, up_reader = self._open_upstream()
            reader = Reader(client)
            name = None
            while True:
                args, raw = reader.command()
                if not args:
                    continue
                verb = args[0].upper()
                if verb == "CLIENT" and len(args) >= 3 and args[1].upper() == "SETNAME":
                    name = args[2]
                    client.sendall(b"+OK\r\n")
                    continue
                if verb == "CLIENT" and len(args) >= 2 and args[1].upper() == "GETNAME":
                    client.sendall(f"${len(name.encode())}\r\n{name}\r\n".encode() if name else b"$-1\r\n")
                    continue
                if verb == "HELLO" and any(a.upper() == "SETNAME" for a in args):
                    i = [a.upper() for a in args].index("SETNAME")
                    name = args[i + 1] if i + 1 < len(args) else name
                    args = args[:i] + args[i + 2 :]
                    raw = encode_command(args)
                if not self._relay(executor_id, client, up, up_reader, args, raw, verb):
                    return
        except (Closed, OSError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            self.sc.errors.append("proxy: " + traceback.format_exc())
        finally:
            for s in (client, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _stored_lease(self, item_id: str):
        raw = self.priv.hget(ITEMS, item_id)
        if raw is None:
            return None
        try:
            lease = json.loads(raw).get("leaseID")
        except ValueError:
            return None
        return lease if isinstance(lease, str) and lease else None

    def _relay(self, executor_id, client, up, up_reader, args, raw, verb) -> bool:
        sc = self.sc
        is_read = verb in READS
        check: Tracked | None = None
        delay_ms = 0
        with sc.lock:
            named = sc.named(args)
            for t in named:
                for a in args[1:]:
                    if looks_like_ulid(a) and a not in (x.item_id for x in sc.items.values()):
                        t.lease_ids.add(a)
                        t.lease_owner.setdefault(a, executor_id)
                        t.lease_seen.setdefault(a, now_ms())
            for t in named:
                if is_read:
                    continue
                if t.kind == "foreign" and not t.fired:
                    t.fired, t.fired_ms, t.fired_by = True, now_ms(), executor_id
                    sc.event("drop", item=t.label, executor=executor_id, cmd=args[:1])
                    self._batch_take(t)
                    drop = True
                    break
                if t.kind == "slow" and t.slow:
                    delay_ms = max(delay_ms, SLOW_CALL_MS)
                if t.kind in ("lost_reply", "kill", "restart") and not t.fired:
                    check = t
            else:
                drop = False
        if drop:
            time.sleep(REPLY_WITHHOLD_MS / 1000)
            return False
        if delay_ms:
            sc.event("delay", executor=executor_id, cmd=args[0], ms=delay_ms)
            time.sleep(delay_ms / 1000)
        if check is None:
            up.sendall(raw)
            reply = up_reader.reply()
            client.sendall(reply)
            return True
        with check.lock:
            before = self._stored_lease(check.item_id) if not check.fired else "x"
            up.sendall(raw)
            reply = up_reader.reply()
            reply_ms = now_ms()
            committed = None
            if before is None and not check.fired:
                committed = self._stored_lease(check.item_id)
            if committed is None:
                client.sendall(reply)
                return True
            check.fired, check.fired_ms, check.fired_by, check.committed_lease = True, reply_ms, executor_id, committed
            check.lease_ids.add(committed)
        sc.event("commit", item=check.label, executor=executor_id, lease=committed, cmd=args[0])
        if check.kind == "restart":
            self._restart(executor_id)
        if check.kind in ("lost_reply", "restart"):
            time.sleep(REPLY_WITHHOLD_MS / 1000)
            sc.event("cut", item=check.label, executor=executor_id)
            return False
        self._kill(executor_id)
        return False

    def _kill(self, executor_id: str) -> None:
        self.sc.killed = executor_id
        _kill_host(self.sc, executor_id)

    def _restart(self, executor_id: str) -> None:
        """A deploy: SIGTERM now, start the same executor again once it has exited."""
        sc = self.sc
        proc = sc.procs.get(executor_id)
        sc.restarted = {"executor": executor_id, "sigterm": now_ms()}
        sc.event("sigterm", executor=executor_id)
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        def redeploy():
            try:
                proc.wait(timeout=GRACEFUL_EXIT_S)
                sc.restarted["exited"] = now_ms()
                sc.event("exited", executor=executor_id, rc=proc.returncode)
            except subprocess.TimeoutExpired:
                sc.restarted["killed_after_grace"] = now_ms()
                sc.event("grace_expired", executor=executor_id)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
            _spawn(sc, executor_id)
            sc.restarted["started"] = now_ms()

        threading.Thread(target=redeploy, daemon=True).start()

    # the batch fleet takes W at the instant the executor's call was dropped
    def _batch_take(self, t: Tracked) -> None:
        lease = ulid_new(now_ms() + BATCH_LEASE_MS)
        try:
            rc = self.batch.fcall("q_lease", 4, ITEMS, CONCURRENCY, f"{{q}}:queue:sorted:{t.partition}",
                                  f"{{q}}:scavenger:{t.partition}", t.item_id, t.partition, lease, now_ms())
        except redis.RedisError as exc:
            rc = repr(exc)
        self.sc.batch_calls.append({"call": "q_lease", "rc": rc, "t": now_ms()})
        t.lease_ids.add(lease)
        t.lease_owner.setdefault(lease, "batch")
        t.lease_seen.setdefault(lease, now_ms())
        if rc != 0:
            return
        run = Run(t.item_id, t.label, "batch", lease, now_ms(), lease)
        self.sc.batch_runs.append(run)
        threading.Thread(target=self._batch_hold, args=(t, lease, run), daemon=True).start()

    def _batch_hold(self, t: Tracked, lease: str, run: Run) -> None:
        client = self.batch
        start = time.monotonic()
        current = lease
        try:
            while time.monotonic() - start < BATCH_HOLD_MS / 1000:
                time.sleep(BATCH_EXTEND_MS / 1000)
                if time.monotonic() - start >= BATCH_HOLD_MS / 1000:
                    break
                nxt = ulid_new(now_ms() + BATCH_LEASE_MS)
                t.lease_ids.add(nxt)
                t.lease_owner.setdefault(nxt, "batch")
                t.lease_seen.setdefault(nxt, now_ms())
                rc = client.fcall("q_extend", 3, ITEMS, CONCURRENCY, f"{{q}}:scavenger:{t.partition}",
                                  t.item_id, current, nxt, t.partition)
                self.sc.batch_calls.append({"call": "q_extend", "rc": rc, "t": now_ms()})
                if rc == 0:
                    current = nxt
            run.end_ms = now_ms()
            rc = client.fcall("q_dequeue", 3, ITEMS, CONCURRENCY, f"{{q}}:scavenger:{t.partition}",
                              t.item_id, t.partition, current)
            self.sc.batch_calls.append({"call": "q_dequeue", "rc": rc, "t": now_ms()})
        except redis.RedisError as exc:
            self.sc.batch_calls.append({"call": "error", "rc": repr(exc), "t": now_ms()})
            run.end_ms = run.end_ms or now_ms()


# ── step runner ───────────────────────────────────────────────────────────────────


class Runner:
    """The step runner as the README describes it, with the platform load balancer in front.

    A run is keyed by (item_id, lease_id). A POST with a new pair starts a run, which carries on to its end
    whatever happens to the request; a POST with a known pair attaches to that run and answers with its
    outcome (at once if it has ended). The load balancer answers 504 to a request that has had no answer
    for LB_IDLE_MS and drops it. F's first run fails (500). When D's first run ends, its executor's host
    dies before the answer is sent.
    """

    def __init__(self, sc: Scenario, priv_factory, on_start):
        self.sc = sc
        self.priv_factory = priv_factory
        self.local = threading.local()
        self.on_start = on_start
        self.by_pair: dict[tuple, Run] = {}
        self.lock = threading.Lock()
        runner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    body = {}
                status = runner.handle(body)
                if status is None:
                    self.close_connection = True
                    return
                out = json.dumps({"ok": status == 200}).encode() if status != 504 else b"Gateway Timeout"
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if status != 504 else "text/plain")
                self.send_header("Content-Length", str(len(out)))
                if status == 504:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _priv(self):
        if not hasattr(self.local, "c"):
            self.local.c = self.priv_factory()
        return self.local.c

    def _stored(self, item_id: str):
        try:
            raw = self._priv().hget(ITEMS, item_id)
            if raw is not None:
                return json.loads(raw).get("leaseID")
        except (redis.RedisError, ValueError):
            pass
        return None

    def handle(self, body: dict):
        sc = self.sc
        at = now_ms()
        item_id = str(body.get("item_id"))
        lease = body.get("lease_id")
        executor = str(body.get("executor_id"))
        t = sc.items.get(item_id)
        with self.lock:
            run = self.by_pair.get((item_id, lease))
            new = run is None
            if new:
                stored = self._stored(item_id)
                run = Run(item_id, t.label if t else "?", executor, lease, at, stored)
                if t is not None and isinstance(stored, str):
                    run.stored_owner = t.lease_owner.get(stored)
                if t is not None and t.kind == "fails" and not any(r.item_id == item_id for r in sc.runs):
                    run.ok = False
                self.by_pair[(item_id, lease)] = run
                with sc.lock:
                    sc.runs.append(run)
        post = {"t": at, "item": run.label, "item_id": item_id, "executor": executor, "lease": lease,
                "new": new, "answer": None}
        with sc.lock:
            sc.posts.append(post)
        if new:
            sc.event("run", item=run.label, executor=executor, lease=lease, stored=run.stored_lease)
            threading.Thread(target=self._execute, args=(t, run), daemon=True).start()
        else:
            sc.event("attach", item=run.label, executor=executor, lease=lease, ended=run.done.is_set())
        if not run.done.wait(LB_IDLE_MS / 1000):
            post["answer"] = 504
            sc.event("lb_timeout", item=run.label, executor=executor, lease=lease)
            return 504
        if run.killed is not None and executor == run.killed and at < (run.end_ms or 0):
            post["answer"] = "lost"
            return None
        post["answer"] = 200 if run.ok else 500
        return post["answer"]

    def _execute(self, t: Tracked | None, run: Run) -> None:
        if t is not None:
            self.on_start(t, run)
        time.sleep((t.step_ms if t else 200) / 1000)
        if t is not None and t.kind == "dies_after_step" and not t.fired:
            # The step has finished; its executor's host dies before the answer gets back.
            stored = self._stored(t.item_id)
            t.fired, t.fired_ms, t.fired_by = True, now_ms(), run.executor
            t.committed_lease = stored if isinstance(stored, str) else None
            run.killed = run.executor
            self.sc.died_after_step = {"executor": run.executor, "t": t.fired_ms, "stored_lease": t.committed_lease}
            _kill_host(self.sc, run.executor)
        run.end_ms = now_ms()
        run.done.set()


# ── the scenario ──────────────────────────────────────────────────────────────────


def _kill_host(sc: Scenario, executor_id: str) -> None:
    """The executor's host dies: SIGKILL its whole process group."""
    proc = sc.procs.get(executor_id)
    sc.event("kill", executor=executor_id)
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _create_cluster(name: str) -> tuple[str, int]:
    ec = boto3.client("elasticache", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    ec.create_cache_cluster(CacheClusterId=name, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1)
    deadline = time.time() + 600
    while time.time() < deadline:
        c = ec.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)["CacheClusters"][0]
        if c["CacheClusterStatus"] == "available" and c.get("CacheNodes"):
            ep = c["CacheNodes"][0]["Endpoint"]
            return ep["Address"], int(ep["Port"])
        time.sleep(1)
    raise RuntimeError("cache cluster never became available")


def _enqueue(priv: redis.Redis, sc: Scenario, items: list[Tracked]) -> None:
    random.shuffle(items)
    at = now_ms()
    pipe = priv.pipeline(transaction=True)
    for t in items:
        body = {"id": t.item_id, "partition": t.partition, "at": at, "leaseID": None, "scavengeCount": 0}
        pipe.hset(ITEMS, t.item_id, json.dumps(body))
        pipe.zadd(f"{{q}}:queue:sorted:{t.partition}", {t.item_id: at})
    with sc.lock:
        for t in items:
            sc.items[t.item_id] = t
    pipe.execute()
    done = now_ms()
    for t in items:
        t.enqueued_ms = done
    sc.event("enqueue", items=[t.label for t in items])


def _new_item(label: str, kind: str) -> Tracked:
    step = STEP_MS[label[0]]
    return Tracked(label, kind, ulid_new(now_ms()), f"fn-{secrets.token_hex(5)}", step)


def run_scenario() -> Scenario:
    sc = Scenario()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _run(sc)
    except Exception:  # noqa: BLE001
        sc.errors.append("scenario: " + traceback.format_exc())
    finally:
        _shutdown(sc)
        _dump(sc)
    return sc


def _run(sc: Scenario) -> None:
    host, port = _create_cluster(f"stepq-verify-{secrets.token_hex(4)}")

    def priv_factory():
        return redis.Redis(host=host, port=port, decode_responses=True, client_name="stepq-harness")

    priv = priv_factory()
    for _ in range(60):
        try:
            priv.ping()
            break
        except redis.ConnectionError:
            time.sleep(1)
    vt = priv.time()
    offset = int(vt[0]) * 1000 + int(vt[1]) // 1000 - now_ms()
    sc.event("clock", valkey_minus_local_ms=offset)

    proxy = Proxy(sc, (host, port), priv_factory)
    scav_client = priv_factory()

    def on_start(t: Tracked, run: Run) -> None:
        if t.kind == "slow" and not t.slow:
            t.slow = True
            sc.event("slow_on", item=t.label)
        if t.kind == "kill" and t.fired and sc.foreign_scavenge is None and run.executor != sc.killed:
            sc.foreign_scavenge = {"scheduled": now_ms()}

            def scavenge():
                time.sleep(FOREIGN_SCAVENGE_AFTER_MS / 1000)
                at = now_ms()
                try:
                    rc = scav_client.fcall("q_scavenge", 4, ITEMS, CONCURRENCY, f"{{q}}:queue:sorted:{t.partition}",
                                           f"{{q}}:scavenger:{t.partition}", t.item_id, t.partition, at)
                except redis.RedisError as exc:
                    rc = repr(exc)
                sc.foreign_scavenge.update({"t": at, "rc": rc})
                sc.event("foreign_scavenge", item=t.label, rc=rc)

            threading.Thread(target=scavenge, daemon=True).start()

    runner = Runner(sc, priv_factory, on_start)

    # A private copy, so the preload never depends on how /tests is mounted.
    clock_lib = Path(tempfile.mkdtemp(prefix="hostclock-")) / HOST_CLOCK_LIB.name
    clock_lib.write_bytes(HOST_CLOCK_LIB.read_bytes())

    for n in range(1, EXECUTORS + 1):
        eid = f"E{n}"
        p = proxy.listen(eid)
        sc.port_owner[p] = eid
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
        env.update({
            "PYTHONPATH": "/app",
            "VALKEY_URL": f"redis://127.0.0.1:{p}/0",
            "STEP_RUNNER_URL": f"http://127.0.0.1:{runner.port}",
            "WORKER_SLOTS": str(WORKER_SLOTS),
            "POLL_MS": str(POLL_MS),
            "PYTHONUNBUFFERED": "1",
        })
        lag_ms = random.randint(*HOST_CLOCK_LAG_MS)
        env.update({"LD_PRELOAD": str(clock_lib), "HOST_CLOCK_SHIFT_MS": str(-lag_ms)})
        sc.clock_lag[eid] = lag_ms
        probe = subprocess.run([sys.executable, "-c", "import time; print(int(time.time() * 1000))"],
                               env=env, capture_output=True, text=True, timeout=30)
        seen = now_ms() - int(probe.stdout.strip() or 0)
        sc.event("host_clock", executor=eid, behind_ms=lag_ms, measured_ms=seen)
        if not lag_ms - 500 <= seen <= lag_ms + 500:
            sc.errors.append(f"scenario: host clock for {eid} not applied (wanted -{lag_ms} ms, saw -{seen} ms)")
            return
        sc.envs[eid] = env
        _spawn(sc, eid)

    deadline = time.time() + 90
    while time.time() < deadline and not all(sc.ready.values()):
        if any(p.poll() is not None for p in sc.procs.values()):
            break
        time.sleep(0.1)
    sc.event("ready", ready=dict(sc.ready))
    if not all(sc.ready.values()):
        sc.errors.append(f"executors not READY: {sc.ready}")
        return
    try:
        sc.functions = sorted(_function_names(priv.function_list()))
    except (redis.RedisError, KeyError, TypeError, ValueError):
        sc.functions = []

    sc.sampling.set()
    threading.Thread(target=_sample, args=(sc, priv_factory()), daemon=True).start()
    sc.t0 = now_ms()

    # phase 1: the lost reply (X) and the slow long step (Y), with plain items, together
    p1 = [_new_item("X", "lost_reply"), _new_item("Y", "slow")]
    p1 += [_new_item(f"N1{i}", "plain") for i in range(random.randint(1, 2))]
    sc.phases["1"] = now_ms()
    _enqueue(priv, sc, list(p1))
    _wait_removed(sc, priv, p1, 45)
    time.sleep(1)

    # phase 2: the item another fleet takes (W) and a step that fails once (F), with plain items
    p2 = [_new_item("W", "foreign"), _new_item("F", "fails")]
    p2 += [_new_item(f"N2{i}", "plain") for i in range(random.randint(1, 2))]
    sc.phases["2"] = now_ms()
    _enqueue(priv, sc, list(p2))
    _wait_removed(sc, priv, p2, 30)
    time.sleep(1)

    # phase 3: the executor restarted by a deploy while its lease of S is unanswered (S), with plain items
    p3 = [_new_item("S", "restart")] + [_new_item(f"N3{i}", "plain") for i in range(random.randint(1, 2))]
    sc.phases["3"] = now_ms()
    _enqueue(priv, sc, list(p3))
    _wait_removed(sc, priv, p3, 30)
    deadline = time.time() + GRACEFUL_EXIT_S + 60
    while time.time() < deadline and sc.restarted and not (sc.restarted.get("started") and all(sc.ready.values())):
        time.sleep(0.1)
    sc.event("redeployed", ready=dict(sc.ready))
    time.sleep(1)

    # phase 4: the holder whose host dies the moment D's step has finished, before it hears so (D)
    p4 = [_new_item("D", "dies_after_step")]
    sc.phases["4"] = now_ms()
    _enqueue(priv, sc, list(p4))
    _wait_removed(sc, priv, p4, 45)
    time.sleep(1)

    # phase 5: the holder that dies as its lease commits (Z)
    p5 = [_new_item("Z", "kill")]
    sc.phases["5"] = now_ms()
    _enqueue(priv, sc, list(p5))
    _wait_removed(sc, priv, p5, 35)
    time.sleep(4)
    sc.phases["end"] = now_ms()

    try:
        sc.final_items = priv.hgetall(ITEMS)
        sc.final_concurrency = priv.zrange(CONCURRENCY, 0, -1, withscores=True)
    except redis.RedisError as exc:
        sc.errors.append(f"final read: {exc!r}")


def _spawn(sc: Scenario, eid: str) -> None:
    """Start executor eid as its own process, from its own empty working directory."""
    cwd = Path(tempfile.mkdtemp(prefix=f"{eid}-"))
    err = open(LOG_DIR / f"executor-{eid}.log", "ab")
    out = open(LOG_DIR / f"executor-{eid}.stdout", "ab")
    sc.ready[eid] = False
    proc = subprocess.Popen([sys.executable, "-m", "stepq.executor", "--id", eid], cwd=cwd, env=sc.envs[eid],
                            stdout=subprocess.PIPE, stderr=err, start_new_session=True)
    sc.procs[eid] = proc

    def watch():
        for line in proc.stdout:
            out.write(line)
            out.flush()
            if line.strip() == b"READY":
                sc.ready[eid] = True
                sc.event("executor_ready", executor=eid)

    threading.Thread(target=watch, daemon=True).start()


def _as_dict(value):
    if isinstance(value, dict):
        return value
    return dict(zip(value[::2], value[1::2]))


def _function_names(libraries) -> list[str]:
    names = []
    for lib in libraries:
        for fn in _as_dict(lib).get("functions") or []:
            names.append(_as_dict(fn)["name"])
    return names


def _wait_removed(sc: Scenario, priv: redis.Redis, items: list[Tracked], timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pending = [t for t in items if t.removed_ms is None]
        if not pending:
            return
        for t in pending:
            if not priv.hexists(ITEMS, t.item_id):
                t.removed_ms = now_ms()
                sc.event("removed", item=t.label)
        time.sleep(0.1)


def _sample(sc: Scenario, client: redis.Redis) -> None:
    last_clients = 0.0
    while sc.sampling.is_set():
        tick = time.monotonic()
        try:
            with sc.lock:
                ids = list(sc.items.keys())
            pipe = client.pipeline(transaction=False)
            pipe.time()
            if ids:
                pipe.hmget(ITEMS, ids)
            res = pipe.execute()
            vt = int(res[0][0]) * 1000 + int(res[0][1]) // 1000
            state = {}
            if ids:
                for item_id, raw in zip(ids, res[1]):
                    if raw is None:
                        state[item_id] = "absent"
                    else:
                        try:
                            state[item_id] = json.loads(raw).get("leaseID")
                        except ValueError:
                            state[item_id] = "corrupt"
            sc.samples.append((now_ms(), vt, state))
            if tick - last_clients > 1.0:
                last_clients = tick
                for c in client.client_list():
                    if c.get("name") in ("stepq-proxy", "stepq-harness"):
                        continue
                    if c.get("cmd") in ("NULL", "client|list", "ping", "info", "hello", "client|setname", ""):
                        continue
                    sc.bypass.append(f"{c.get('addr')} name={c.get('name')!r} cmd={c.get('cmd')}")
        except Exception as exc:  # noqa: BLE001 - a dead sampler would grade nothing
            sc.errors.append(f"sampler: {exc!r}")
        time.sleep(max(0.0, SAMPLE_MS / 1000 - (time.monotonic() - tick)))


def _shutdown(sc: Scenario) -> None:
    sc.sampling.clear()
    for proc in sc.procs.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.time() + 8
    for proc in sc.procs.values():
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            pass
    for proc in sc.procs.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _lease_changes(sc: Scenario) -> dict:
    """Per item: (local ms, valkey ms, stored lease) at every sample where the stored lease changed."""
    labels = {t.item_id: t.label for t in sc.items.values()}
    last: dict = {}
    out: dict = {}
    for at, vt, state in sc.samples:
        for item_id, lease in state.items():
            if last.get(item_id, "-") != lease:
                last[item_id] = lease
                out.setdefault(labels.get(item_id, item_id), []).append((at, vt, lease))
    return out


def _dump(sc: Scenario) -> None:
    try:
        out = {
            "t0": sc.t0,
            "phases": sc.phases,
            "errors": sc.errors,
            "items": {t.label: {"id": t.item_id, "partition": t.partition, "kind": t.kind, "enqueued": t.enqueued_ms,
                                "fired": t.fired_ms, "fired_by": t.fired_by, "committed_lease": t.committed_lease,
                                "removed": t.removed_ms} for t in sc.items.values()},
            "runs": [r.facts() for r in sc.runs],
            "posts": sc.posts,
            "batch_runs": [r.facts() for r in sc.batch_runs],
            "batch_calls": sc.batch_calls,
            "foreign_scavenge": sc.foreign_scavenge,
            "killed": sc.killed,
            "died_after_step": sc.died_after_step,
            "restarted": sc.restarted,
            "clock_lag_ms": sc.clock_lag,
            "bypass": sc.bypass[:50],
            "functions": sc.functions,
            "samples": len(sc.samples),
            "lease_changes": _lease_changes(sc),
            "events": sc.events,
        }
        (LOG_DIR / "scenario.json").write_text(json.dumps(out, indent=1, default=str))
    except Exception:  # noqa: BLE001
        pass
