"""Verifier rig: a Valkey cluster through ElastiCache, an observer of executing jobs, one RESP-aware
TCP proxy per pool, and the timeline that starts, kills and faults the pools under test.

Nothing here imports /app: the pools are black boxes started as `/app/bin/pool`, and everything graded
comes from Valkey (read directly), the observer (fed by our own jobrun) and the proxies.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import boto3
import redis
from botocore.config import Config as BotoConfig

NAMESPACE = "work"
JOB_TYPE = "process"
MAX_CONCURRENCY = 3
HEARTBEAT_PERIOD_S = 1
DEAD_TIME_S = 4
REAP_PERIOD_S = 1
REAP_JITTER_S = 1

LONG_JOB_MS = 40_000
# the job P4 holds when its pool process alone is killed: its runner outlives its pool and must finish
GUARD_JOB_MS = 20_000
SHORT_JOB_MS = 400
BACKLOG_JOBS = 300
READY_TIMEOUT_S = 15
LONG_CLAIM_TIMEOUT_S = 10
KILL_WAIT_S = 15
SAMPLE_S = 0.1
RUN_DEADLINE_S = 150
QUIESCE_S = 8
RECLAIM_DEADLINE_S = 10
GRACEFUL_STOP_S = 5
FAULT_MISS_S = 3.0
DROP_WINDOW_S = 0.25
FAULT_REARM_MAX = 5
# (seconds after the kill, kind): release faults arm on the first P2 completion after the time and hit
# the writes P2 sends next (see Proxy._release_action); the claim fault hits the first reply that tells
# P2 about a job it has just been given.
FAULT_SCHEDULE = [(8, "drop_request"), (10, "drop_reply"), (12, "drop_request"), (14, "drop_reply"), (16, "claim_reply")]
# Not graded as a fault: when P4's orphaned runner ends, the reply to the first write through P4's relay
# that takes its job out of P4's in-progress list is lost (designs that never write there skip it).

KEY_QUEUE = f"{NAMESPACE}:jobs:{JOB_TYPE}"
KEY_LOCK = f"{KEY_QUEUE}:lock"
KEY_LOCK_INFO = f"{KEY_QUEUE}:lock_info"
KEY_POOLS = f"{NAMESPACE}:worker_pools"


def key_inprogress(pool_id: str) -> str:
    return f"{KEY_QUEUE}:{pool_id}:inprogress"


READ_ONLY = {
    b"GET", b"MGET", b"EXISTS", b"TYPE", b"TTL", b"PTTL", b"STRLEN", b"LRANGE", b"LINDEX", b"LLEN", b"LPOS",
    b"HGET", b"HMGET", b"HGETALL", b"HEXISTS", b"HKEYS", b"HVALS", b"HLEN", b"SMEMBERS", b"SISMEMBER",
    b"SMISMEMBER", b"SCARD", b"ZRANGE", b"ZSCORE", b"ZCARD", b"ZRANGEBYSCORE", b"ZCOUNT", b"SCAN", b"SSCAN",
    b"HSCAN", b"ZSCAN", b"KEYS", b"PING", b"ECHO", b"EVAL_RO", b"EVALSHA_RO", b"FCALL_RO", b"SCRIPT", b"CLIENT",
    b"INFO", b"HELLO", b"SELECT", b"AUTH", b"COMMAND", b"WATCH", b"UNWATCH", b"DISCARD", b"QUIT", b"RESET",
}


class EnvironmentDown(RuntimeError):
    """The emulator or engine did not come up: an infrastructure failure, not a solver failure."""


# ── RESP framing ──────────────────────────────────────────────────────────────────


def _resp_value_end(buf: bytes, pos: int) -> int | None:
    """End offset of the RESP value at pos, or None while it is incomplete."""
    if pos >= len(buf):
        return None
    nl = buf.find(b"\r\n", pos)
    if nl < 0:
        return None
    kind = buf[pos : pos + 1]
    line = buf[pos + 1 : nl]
    after = nl + 2
    if kind in (b"+", b"-", b":", b"_", b",", b"#", b"("):
        return after
    if kind in (b"$", b"=", b"!"):
        size = int(line)
        if size < 0:
            return after
        end = after + size + 2
        return end if len(buf) >= end else None
    if kind in (b"*", b"~", b">", b"%", b"|"):
        count = int(line)
        if count < 0:
            return after
        if kind in (b"%", b"|"):
            count *= 2
        p: int | None = after
        for _ in range(count):
            p = _resp_value_end(buf, p)
            if p is None:
                return None
        return p
    raise ValueError(f"bad RESP type {kind!r}")


def parse_reply(buf: bytes) -> tuple[int, bool] | None:
    """(end, is_push) of the first complete top-level reply in buf."""
    if not buf:
        return None
    pos = 0
    if buf[:1] == b"|":  # an attribute precedes the reply it annotates
        pos = _resp_value_end(buf, 0)
        if pos is None:
            return None
    end = _resp_value_end(buf, pos)
    if end is None:
        return None
    return end, buf[pos : pos + 1] == b">"


def parse_command(buf: bytes) -> tuple[int, list[bytes]] | None:
    """(end, args) of the first complete client command in buf."""
    if not buf:
        return None
    if buf[:1] != b"*":  # inline command
        nl = buf.find(b"\n")
        if nl < 0:
            return None
        return nl + 1, buf[:nl].strip().split()
    nl = buf.find(b"\r\n")
    if nl < 0:
        return None
    count = int(buf[1:nl])
    pos = nl + 2
    args = []
    for _ in range(max(count, 0)):
        nl = buf.find(b"\r\n", pos)
        if nl < 0:
            return None
        if buf[pos : pos + 1] != b"$":
            raise ValueError("bad command frame")
        size = int(buf[pos + 1 : nl])
        start = nl + 2
        if len(buf) < start + size + 2:
            return None
        args.append(buf[start : start + size])
        pos = start + size + 2
    return pos, args


# ── observer ──────────────────────────────────────────────────────────────────────


class Observer:
    """Ground truth for what is executing: every jobrun reports START and END and waits for our ack."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        # one entry per live runner, "<job id>#<runner pid>" -> pool id: two runners of one job count twice
        self.executing: dict[str, str] = {}
        self.starts: dict[str, int] = {}
        self.ends: dict[str, float] = {}
        self.end_counts: dict[str, int] = {}
        self.killed: set[str] = set()
        self.events: list[tuple[float, str, str, str, int]] = []  # (t, kind, job, pool, executing after)
        self.max_executing = 0
        self.over_admissions: list[tuple[float, list[tuple[str, str]]]] = []
        self.on_end = None  # callback(job_id, pool_id) run before the END is acknowledged
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(256)
        threading.Thread(target=self._accept, daemon=True).start()

    def snapshot(self) -> dict[str, str]:
        with self.lock:
            return dict(self.executing)

    @staticmethod
    def job_of(run: str) -> str:
        return run.split("#", 1)[0]

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        job_id = pool_id = run = None
        ended = False
        reader = conn.makefile("rb")
        try:
            for raw in reader:
                parts = raw.decode().split()
                if not parts:
                    continue
                if parts[0] == "START":
                    job_id, pool_id = parts[1], parts[2]
                    run = f"{job_id}#{parts[3] if len(parts) > 3 else id(conn)}"
                    with self.lock:
                        self.executing[run] = pool_id
                        self.starts[job_id] = self.starts.get(job_id, 0) + 1
                        n = len(self.executing)
                        self.max_executing = max(self.max_executing, n)
                        self.events.append((time.time(), "START", job_id, pool_id, n))
                        if n > MAX_CONCURRENCY:
                            self.over_admissions.append((time.time(), sorted(self.executing.items())))
                    conn.sendall(b"ok\n")
                elif parts[0] == "END" and job_id:
                    with self.lock:
                        self.executing.pop(run, None)
                        self.ends.setdefault(job_id, time.time())
                        self.end_counts[job_id] = self.end_counts.get(job_id, 0) + 1
                        self.events.append((time.time(), "END", job_id, pool_id, len(self.executing)))
                    ended = True
                    if self.on_end:
                        with contextlib.suppress(Exception):
                            self.on_end(job_id, pool_id)
                    conn.sendall(b"ok\n")
        except OSError:
            pass
        finally:
            if job_id and not ended:
                with self.lock:
                    self.executing.pop(run, None)
                    self.killed.add(job_id)
                    self.events.append((time.time(), "GONE", job_id, pool_id or "", len(self.executing)))
            with contextlib.suppress(OSError):
                conn.close()


JOBRUN = r'''#!{python} -S
import os, socket, sys, time
argv = sys.argv[1:]
opts = dict(zip(argv[0::2], argv[1::2]))
job, pool, ms = opts["--job-id"], opts["--pool-id"], int(opts.get("--duration-ms", "0"))
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect({sock!r})
f = s.makefile("rb")
s.sendall(("START %s %s %d\n" % (job, pool, os.getpid())).encode())
f.readline()
deadline = time.monotonic() + ms / 1000.0
while True:
    left = deadline - time.monotonic()
    if left <= 0:
        break
    time.sleep(min(0.05, left))
s.sendall(("END %s\n" % job).encode())
f.readline()
sys.exit(0)
'''


# ── proxy ─────────────────────────────────────────────────────────────────────────


class Proxy:
    """A TCP relay to Valkey for one pool. Faults are armed by the rig; unarmed it forwards verbatim."""

    def __init__(self, rig: "Rig", pool_id: str, upstream: tuple[str, int]) -> None:
        self.rig = rig
        self.pool_id = pool_id
        self.upstream = upstream
        self.lock = threading.Lock()
        self.release_fault: dict | None = None  # {"kind", "job_id", "armed_at", ...}
        self.probe_lock = threading.Lock()  # one write at a time while a drop_reply is armed
        self.claim_fault: dict | None = None
        self.seen: set[bytes] = set()  # job ids this pool has sent or been sent
        self.connections = 0
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(256)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.upstream, timeout=5)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._relay, args=(client, upstream), daemon=True).start()

    @staticmethod
    def _reset(client: socket.socket, upstream: socket.socket) -> None:
        with contextlib.suppress(OSError):
            upstream.close()
        with contextlib.suppress(OSError):
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        with contextlib.suppress(OSError):
            client.close()

    def _release_action(self, cmd: bytes) -> tuple[str, dict] | None:
        """What an armed release fault does to this write: ("drop", fault) or ("probe", fault).

        A release fault is armed when P2 finishes a job, before the runner exits. It does not look for
        any particular command: drop_request resets every write P2 sends for DROP_WINDOW_S (at least
        one), as a relay restart would; drop_reply lets P2's writes through one at a time and loses the
        reply of the one whose execution took the finished job out of P2's in-progress list."""
        if cmd in READ_ONLY:
            return None
        with self.lock:
            fault = self.release_fault
            if fault is None:
                return None
            if fault["kind"] == "drop_request":
                if fault.get("dropped", 0) and time.time() >= fault["armed_at"] + DROP_WINDOW_S:
                    self.release_fault = None
                    return None
                fault["dropped"] = fault.get("dropped", 0) + 1
                return "drop", fault
        if not self.probe_lock.acquire(timeout=5):
            return None
        with self.lock:
            if self.release_fault is not fault:
                self.probe_lock.release()
                return None
        return "probe", fault

    def _take_release(self, fault: dict) -> bool:
        with self.lock:
            if self.release_fault is not fault:
                return False
            self.release_fault = None
            return True

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        cbuf = b""
        sbuf = b""
        sent = replies = 0
        in_multi = False
        # a drop_reply probe in flight on this connection: the frames of one write (through EXEC inside
        # MULTI) are forwarded, their replies held until Valkey has answered all of them
        probe: dict | None = None
        jobid = self.rig.job_id_re

        def end_probe() -> None:
            nonlocal probe
            probe = None
            with contextlib.suppress(RuntimeError):
                self.probe_lock.release()

        try:
            while True:
                pending = probe is None and parse_command(cbuf) is not None
                readable, _, _ = select.select([client, upstream], [], [], 0 if pending else 0.2)
                if probe is not None and time.time() > probe["deadline"]:
                    self.rig.fault_done(probe["fault"], "reply wait timed out")
                    end_probe()
                    self._reset(client, upstream)
                    return
                if client in readable:
                    data = client.recv(65536)
                    if not data:
                        break
                    cbuf += data
                while (probe is None or probe["phase"] == "queue") and (parsed := parse_command(cbuf)) is not None:
                    end, args = parsed
                    frame, cbuf = cbuf[:end], cbuf[end:]
                    cmd = args[0].upper() if args else b""
                    ids = set(jobid.findall(frame))
                    with self.lock:
                        self.seen |= ids
                    if probe is None:
                        action = self._release_action(cmd)
                        if action is not None and action[0] == "drop":
                            fault = action[1]
                            if fault["dropped"] == 1:
                                self.rig.fault_fired(fault, cmd.decode(errors="replace"), in_multi)
                            self._reset(client, upstream)
                            return
                        if action is not None:
                            probe = dict(fault=action[1], start=sent, phase="queue", deadline=time.time() + 5,
                                         cmd=cmd.decode(errors="replace"), in_multi=in_multi, held=[])
                    upstream.sendall(frame)
                    sent += 1
                    if cmd == b"MULTI":
                        in_multi = True
                    elif cmd in (b"EXEC", b"DISCARD"):
                        in_multi = False
                    if probe is not None and probe["phase"] == "queue" and not in_multi:
                        probe["phase"], probe["cutoff"] = "wait", sent
                if upstream in readable:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    sbuf += data
                while (parsed := parse_reply(sbuf)) is not None:
                    end, push = parsed
                    reply, sbuf = sbuf[:end], sbuf[end:]
                    if push:
                        if probe is None:
                            client.sendall(reply)
                        continue
                    index = replies
                    replies += 1
                    if probe is not None and index >= probe["start"]:
                        probe["held"].append(reply)
                        if probe["phase"] == "wait" and replies >= probe["cutoff"]:
                            fault = probe["fault"]
                            held = self.rig.in_progress_of(self.pool_id, strict=True)
                            if held is not None and fault["job_id"].encode() not in held and self._take_release(fault):
                                self.rig.fault_fired(fault, probe["cmd"], probe["in_multi"])
                                self.rig.fault_done(fault, "reply discarded")
                                end_probe()
                                self._reset(client, upstream)
                                return
                            with self.lock:
                                for r in probe["held"]:
                                    self.seen |= set(jobid.findall(r))
                            client.sendall(b"".join(probe["held"]))
                            end_probe()
                        continue
                    ids = set(jobid.findall(reply))
                    if ids and self._claim_reply(ids):
                        self._reset(client, upstream)
                        return
                    client.sendall(reply)
        except (OSError, ValueError):
            pass
        finally:
            if probe is not None:
                end_probe()
        for sock in (client, upstream):
            with contextlib.suppress(OSError):
                sock.close()

    def _claim_reply(self, ids: set[bytes]) -> bool:
        """Drop this reply if it is the first news to the pool of a job just moved to its in-progress list."""
        with self.lock:
            fault = self.claim_fault
            new = ids - self.seen
            self.seen |= ids
            if fault is None or not new:
                return False
        claimed = self.rig.in_progress_of(self.pool_id)
        for job in new:
            if job in claimed:
                with self.lock:
                    if self.claim_fault is not fault:
                        return False
                    self.claim_fault = None
                fault["job_id"] = job.decode()
                self.rig.fault_fired(fault, "claim reply", False)
                self.rig.fault_done(fault, "reply discarded")
                return True
        return False


# ── the run ───────────────────────────────────────────────────────────────────────


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat[stat.rfind(b")") + 2 :].split()
        children.setdefault(int(fields[1]), []).append(int(entry))
    found, todo = [], [root]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found


def pods_processes(pod: str) -> set[int]:
    """Processes that inherited this pod's marker, the closest thing here to the pod's cgroup."""
    marker = f"RIG_POD={pod}".encode()
    found = set()
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            with contextlib.suppress(OSError), open(f"/proc/{entry}/environ", "rb") as f:
                if marker in f.read().split(b"\0"):
                    found.add(int(entry))
    return found


class PoolProcess:
    def __init__(self, role: str, pool_id: str, workers: int, proxy: Proxy, workdir: str, env: dict) -> None:
        self.role = role
        self.pool_id = pool_id
        self.ready = threading.Event()
        self.stdout_lines: list[str] = []
        self.stderr_path = os.path.join(workdir, f"{role}.stderr")
        self.signalled = False
        self.exit_seen: float | None = None
        self.stderr = open(self.stderr_path, "wb")
        self.pod = f"{env.get('RIG_RUN', '')}-{role}"
        self.proc = subprocess.Popen(
            ["/app/bin/pool", "--pool-id", pool_id, "--workers", str(workers)],
            cwd=workdir,
            env={**env, "VALKEY_HOST": "127.0.0.1", "VALKEY_PORT": str(proxy.port), "RIG_POD": self.pod},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            start_new_session=True,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            if len(self.stdout_lines) < 2000:
                self.stdout_lines.append(line)
            if line.strip() == f"READY {self.pool_id}":
                self.ready.set()

    def alive(self) -> bool:
        if self.proc.poll() is None:
            return True
        if self.exit_seen is None:
            self.exit_seen = time.time()
        return False

    def kill(self) -> None:
        """What a deploy does: SIGKILL to every process in the pod (its process group and anything the
        pool started, wherever it moved)."""
        self.signalled = True
        tree = set(descendants(self.proc.pid)) | pods_processes(self.pod)
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        for pid in tree:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)

    def kill_pool_process(self) -> None:
        """What the node's memory guard does: SIGKILL to the pool process, nothing else in the pod."""
        self.signalled = True
        with contextlib.suppress(OSError):
            os.kill(self.proc.pid, signal.SIGKILL)

    def terminate(self) -> None:
        self.signalled = True
        with contextlib.suppress(OSError):
            os.kill(self.proc.pid, signal.SIGTERM)

    def stderr_tail(self, size: int = 3000) -> str:
        with contextlib.suppress(OSError), open(self.stderr_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode(errors="replace")
        return ""


class Rig:
    def __init__(self) -> None:
        self.tag = secrets.token_hex(3)
        self.job_id_re = re.compile(rb"j" + self.tag.encode() + rb"x[0-9]{4}")
        self.workdir = tempfile.mkdtemp(prefix="rig-")
        self.error: str | None = None
        self.environment_error: str | None = None
        self.notes: list[str] = []
        self.samples: list[tuple[float, frozenset, int, int, dict, frozenset]] = []
        self.faults: list[dict] = []
        self.fault_lock = threading.Lock()
        self.pools: dict[str, PoolProcess] = {}
        self.pool_ids: dict[str, str] = {}
        self.job_ids: list[str] = []
        self.long_job = ""
        self.guard_job = ""
        self.orphan_fault: dict = {}
        self.t_end = 0.0
        self.t_kill = 0.0
        self.t_backlog = 0.0
        self.killed_jobs: set[str] = set()
        self.final_state: dict | None = None
        self.quiesced = False
        self.unexpected_exits: list[str] = []
        self.completed_all = False
        self.cluster_id = f"verify-{self.tag}"
        self._stop_sampling = threading.Event()
        self.timeline: list[str] = []

    # helpers used by the proxies and the observer

    def log(self, message: str) -> None:
        self.timeline.append(f"{time.time() - self.t0:7.2f}s {message}")

    def in_progress_of(self, pool_id: str, strict: bool = False) -> set[bytes] | None:
        try:
            entries = self.direct_side.lrange(key_inprogress(pool_id), 0, -1)
        except redis.RedisError:
            return None if strict else set()
        return set(self.job_id_re.findall(b"".join(entries)))

    def fault_fired(self, fault: dict, command: str, in_multi: bool) -> None:
        with self.fault_lock:
            fault.update(state="fired", command=command, in_multi=in_multi, fired_at=time.time())
        self.log(f"fault {fault['kind']} fired on {command} (job {fault.get('job_id')}, in MULTI: {in_multi})")

    def fault_done(self, fault: dict | None, how: str) -> None:
        if fault is not None:
            fault["result"] = how

    def rig_done_drop(self, fault: dict) -> None:
        self.fault_done(fault, f"{fault.get('dropped', 0)} request(s) reset")
        self.log(f"drop_request window after {fault['job_id']} closed, {fault.get('dropped', 0)} request(s) reset")

    # Valkey

    def create_valkey(self) -> None:
        client = boto3.client("elasticache", config=BotoConfig(read_timeout=600, retries={"max_attempts": 1}))
        try:
            client.create_cache_cluster(CacheClusterId=self.cluster_id, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1)
        except Exception as error:  # noqa: BLE001
            raise EnvironmentDown(f"create_cache_cluster failed: {error}") from error
        deadline = time.time() + 600
        endpoint = None
        while time.time() < deadline:
            with contextlib.suppress(Exception):
                cluster = client.describe_cache_clusters(CacheClusterId=self.cluster_id, ShowCacheNodeInfo=True)["CacheClusters"][0]
                nodes = cluster.get("CacheNodes") or []
                if cluster.get("CacheClusterStatus") == "available" and nodes and nodes[0].get("Endpoint"):
                    endpoint = nodes[0]["Endpoint"]
                    break
            time.sleep(1)
        if endpoint is None:
            raise EnvironmentDown("ElastiCache cluster did not become available")
        host = endpoint["Address"]
        if host in ("localhost", "::1") or host.startswith("127."):
            host = "127.0.0.1"
        self.valkey = (host, int(endpoint["Port"]))
        deadline = time.time() + 60
        while True:
            try:
                self.direct = redis.Redis(host=host, port=self.valkey[1], socket_timeout=10)
                self.direct.ping()
                break
            except redis.RedisError as error:
                if time.time() > deadline:
                    raise EnvironmentDown(f"Valkey at {self.valkey} not reachable: {error}") from error
                time.sleep(1)
        self.direct.flushall()
        self.direct_side = redis.Redis(host=host, port=self.valkey[1], socket_timeout=10)
        self.sampler_client = redis.Redis(host=host, port=self.valkey[1], socket_timeout=10)

    def delete_valkey(self) -> None:
        with contextlib.suppress(Exception):
            boto3.client("elasticache").delete_cache_cluster(CacheClusterId=self.cluster_id)

    # jobs

    def job(self, n: int, duration_ms: int) -> tuple[str, bytes]:
        job_id = f"j{self.tag}x{n:04d}"
        body = {"name": JOB_TYPE, "id": job_id, "t": int(time.time()), "args": {"duration_ms": duration_ms}}
        return job_id, json.dumps(body, separators=(",", ":")).encode()

    # sampling

    def _sample(self) -> None:
        pipe_keys = (KEY_LOCK, KEY_QUEUE, KEY_LOCK_INFO)
        while not self._stop_sampling.is_set():
            started = time.time()
            s1 = frozenset(self.observer.snapshot())
            try:
                pipe = self.sampler_client.pipeline(transaction=True)
                pipe.get(pipe_keys[0])
                pipe.llen(pipe_keys[1])
                pipe.hgetall(pipe_keys[2])
                lock, llen, info = pipe.execute()
            except redis.RedisError as error:
                self.notes.append(f"sample failed: {error}")
                time.sleep(SAMPLE_S)
                continue
            s2 = frozenset(self.observer.snapshot())
            lock_value = int(lock) if lock is not None else 0
            info_values = {k.decode(): int(v) for k, v in info.items()}
            self.samples.append((started, s1, lock_value, int(llen), info_values, s2))
            time.sleep(max(0.0, SAMPLE_S - (time.time() - started)))

    # the release-fault schedule

    def _on_end(self, job_id: str, pool_id: str) -> None:
        if job_id == self.guard_job and pool_id == self.pool_ids.get("P4") and self.t_kill:
            self._arm_orphan_fault()
            return
        if pool_id != self.pool_ids.get("P2") or not self.t_kill:
            return
        now = time.time()
        with self.fault_lock:
            if any(f["state"] == "armed" and f["kind"] != "claim_reply" and f["role"] == "P2" for f in self.faults):
                return
            with self.proxies["P2"].lock:
                if self.proxies["P2"].release_fault is not None:
                    return
            for fault in self.faults:
                if fault["kind"] == "claim_reply" or fault["role"] != "P2" or fault["state"] not in ("pending", "missed"):
                    continue
                if now < self.t_kill + fault["at"]:
                    return
                fault.update(state="armed", job_id=job_id, armed_at=now)
                with self.proxies["P2"].lock:
                    self.proxies["P2"].release_fault = fault
                self.log(f"armed {fault['kind']} on completion of {job_id}")
                return

    def _arm_orphan_fault(self) -> None:
        fault = self.orphan_fault
        held = self.in_progress_of(self.pool_ids["P4"], strict=True)
        with self.fault_lock:
            if fault["state"] != "pending":
                return
            if not held or self.guard_job.encode() not in held:
                fault.update(state="skipped", result="the job was not in P4's in-progress list when its runner ended")
                return
            fault.update(state="armed", job_id=self.guard_job, armed_at=time.time())
            with self.proxies["P4"].lock:
                self.proxies["P4"].release_fault = fault
        self.log(f"armed {fault['kind']} on P4's relay at the end of {self.guard_job}")

    def _fault_clock(self) -> None:
        while not self._stop_sampling.is_set():
            now = time.time()
            with self.fault_lock:
                for fault in self.faults:
                    if fault["kind"] == "claim_reply" and fault["state"] == "pending" and now >= self.t_kill + fault["at"]:
                        fault.update(state="armed", armed_at=now)
                        with self.proxies["P2"].lock:
                            self.proxies["P2"].claim_fault = fault
                        self.log("armed claim_reply")
                    elif fault["kind"] == "drop_request" and fault["state"] == "fired" and now >= fault["armed_at"] + DROP_WINDOW_S:
                        with self.proxies["P2"].lock:
                            if self.proxies["P2"].release_fault is fault:
                                self.proxies["P2"].release_fault = None
                        if fault.get("result") is None:
                            self.rig_done_drop(fault)
                    elif fault["kind"] != "claim_reply" and fault["state"] == "armed" and now > fault["armed_at"] + FAULT_MISS_S:
                        proxy = self.proxies[fault["role"]]
                        with proxy.lock:
                            if proxy.release_fault is fault:
                                proxy.release_fault = None
                        fault["misses"] += 1
                        if fault["role"] != "P2":
                            fault["state"] = "missed"
                        else:
                            fault["state"] = "missed" if fault["misses"] < FAULT_REARM_MAX else "failed"
                        self.log(f"{fault['kind']} on {fault['job_id']} saw no write it could reset ({fault['misses']})")
            time.sleep(0.1)

    # the timeline

    def execute(self) -> None:
        self.t0 = time.time()
        self.create_valkey()
        self.log(f"valkey at {self.valkey}")
        self.observer = Observer(os.path.join(self.workdir, "observer.sock"))
        self.observer.on_end = self._on_end
        jobrun = os.path.join(self.workdir, "jobrun")
        with open(jobrun, "w") as f:
            f.write(JOBRUN.format(python=sys.executable, sock=self.observer.path))
        os.chmod(jobrun, 0o755)
        for role in ("P1", "P2", "P3", "P4"):
            self.pool_ids[role] = secrets.token_hex(12)
        self.proxies = {role: Proxy(self, pid, self.valkey) for role, pid in self.pool_ids.items()}
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "VALKEY_"))
        }
        env.update(
            NAMESPACE=NAMESPACE,
            JOB_TYPE=JOB_TYPE,
            MAX_CONCURRENCY=str(MAX_CONCURRENCY),
            HEARTBEAT_PERIOD_S=str(HEARTBEAT_PERIOD_S),
            DEAD_TIME_S=str(DEAD_TIME_S),
            REAP_PERIOD_S=str(REAP_PERIOD_S),
            REAP_JITTER_S=str(REAP_JITTER_S),
            JOBRUN=jobrun,
            RIG_RUN=self.tag,
            PYTHONPATH="/app",
            PYTHONUNBUFFERED="1",
        )
        self.env = env
        self.faults = [dict(at=at, kind=kind, role="P2", graded=True, state="pending", misses=0) for at, kind in FAULT_SCHEDULE]
        self.orphan_fault = dict(at=None, kind="drop_reply", role="P4", graded=False, state="pending", misses=0)
        self.faults.append(self.orphan_fault)
        threading.Thread(target=self._sample, daemon=True).start()

        self.start_pool("P3", 1)
        self.long_job, raw = self.job(0, LONG_JOB_MS)
        self.job_ids.append(self.long_job)
        self.direct.lpush(KEY_QUEUE, raw)
        self.wait_for(lambda: self.running_on(self.long_job) == [self.pool_ids["P3"]], LONG_CLAIM_TIMEOUT_S,
                      "the long job was not running on P3 within the claim timeout")
        self.log(f"long job {self.long_job} running on P3")
        self.guard_job, raw = self.job(BACKLOG_JOBS + 1, GUARD_JOB_MS)
        self.job_ids.append(self.guard_job)
        self.direct.lpush(KEY_QUEUE, raw)
        self.start_pool("P4", 1)
        self.wait_for(lambda: self.running_on(self.guard_job) == [self.pool_ids["P4"]], LONG_CLAIM_TIMEOUT_S,
                      "the 20 s job was not running on P4 within the claim timeout")
        self.log(f"job {self.guard_job} running on P4")
        backlog = [self.job(n, SHORT_JOB_MS) for n in range(1, BACKLOG_JOBS + 1)]
        self.job_ids += [job_id for job_id, _ in backlog]
        self.direct.lpush(KEY_QUEUE, *[raw for _, raw in backlog])
        self.t_backlog = time.time()
        self.start_pool("P1", 1)

        def split() -> bool:
            held = list(self.observer.snapshot().values())
            return held.count(self.pool_ids["P1"]) == 1 and held.count(self.pool_ids["P4"]) == 1

        self.wait_for(split, KILL_WAIT_S, "P1 and P4 never each had a job executing", poll=0.05)
        snapshot = self.observer.snapshot()
        self.pools["P1"].kill()
        self.pools["P4"].kill_pool_process()
        self.t_kill = time.time()
        # only the deploy's kill may cut a runner off; P4's runner carries on after its pool process
        self.killed_jobs = {Observer.job_of(r) for r, p in snapshot.items() if p == self.pool_ids["P1"]}
        self.log(f"killed P1's whole pod and P4's pool process; P1 held {sorted(self.killed_jobs)}, "
                 f"P4 held {self.guard_job}")
        threading.Thread(target=self._fault_clock, daemon=True).start()
        self.start_pool("P2", 4)

        deadline = self.t_backlog + RUN_DEADLINE_S
        while time.time() < deadline:
            for role in ("P2", "P3"):
                pool = self.pools[role]
                if not pool.alive() and f"{role}" not in self.unexpected_exits:
                    self.unexpected_exits.append(role)
                    self.log(f"{role} exited with {pool.proc.returncode}")
            with self.observer.lock:
                # a job can end once and still have a second runner going (a reaped pool's runner, then the
                # requeued copy): the run is over when every job has ended and no runner is left
                done = all(j in self.observer.ends for j in self.job_ids) and not self.observer.executing
            if done:
                self.completed_all = True
                break
            time.sleep(0.2)
        self.t_end = time.time()
        self.log(f"run ended, all completed: {self.completed_all}")
        quiesce_deadline = time.time() + QUIESCE_S
        while True:
            self.final_state = self.read_state()
            if self.state_settled(self.final_state) or time.time() > quiesce_deadline:
                break
            time.sleep(0.2)
        self.quiesced = self.state_settled(self.final_state)
        for role in ("P2", "P3"):
            if not self.pools[role].alive() and role not in self.unexpected_exits:
                self.unexpected_exits.append(role)
        self.log(f"final state {self.final_state}")

    def start_pool(self, role: str, workers: int) -> None:
        pool = PoolProcess(role, self.pool_ids[role], workers, self.proxies[role], self.workdir, self.env)
        self.pools[role] = pool
        if not pool.ready.wait(READY_TIMEOUT_S):
            raise AssertionError(f"pool {role} did not print READY {pool.pool_id} within {READY_TIMEOUT_S}s; "
                                 f"stderr tail:\n{pool.stderr_tail()}")
        self.log(f"{role} ({pool.pool_id}) ready")

    def running_on(self, job_id: str) -> list[str]:
        return sorted(p for r, p in self.observer.snapshot().items() if Observer.job_of(r) == job_id)

    def wait_for(self, predicate, timeout: float, message: str, poll: float = 0.1) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(poll)
        raise AssertionError(message)

    def read_state(self) -> dict:
        r = self.direct_side
        lock = r.get(KEY_LOCK)
        info = {k.decode(): int(v) for k, v in r.hgetall(KEY_LOCK_INFO).items()}
        inprogress = {}
        for key in r.scan_iter(match=f"{KEY_QUEUE}:*:inprogress", count=1000):
            inprogress[key.decode()] = r.llen(key)
        pools = sorted(p.decode() for p in r.smembers(KEY_POOLS))
        return {
            "lock": None if lock is None else int(lock),
            "lock_info": info,
            "inprogress": inprogress,
            "worker_pools": pools,
            "queue": r.llen(KEY_QUEUE),
        }

    def state_settled(self, state: dict) -> bool:
        return (
            state["lock"] in (None, 0)
            and all(v == 0 for v in state["lock_info"].values())
            and all(n == 0 for n in state["inprogress"].values())
            and state["worker_pools"] == sorted([self.pool_ids["P2"], self.pool_ids["P3"]])
        )

    def teardown(self) -> None:
        self._stop_sampling.set()
        for pool in self.pools.values():
            if pool.alive():
                pool.terminate()
        deadline = time.time() + GRACEFUL_STOP_S
        for pool in self.pools.values():
            with contextlib.suppress(subprocess.TimeoutExpired):
                pool.proc.wait(max(0.1, deadline - time.time()))
            pool.kill()
        self.delete_valkey()

    def cut_off_runs(self) -> list[tuple[float, str, str]]:
        """Runners that went away without finishing their job while the run was on, other than the deploy's."""
        end = self.t_end or time.time()
        with self.observer.lock:
            return [(round(t - self.t0, 2), job, pool) for t, kind, job, pool, _n in self.observer.events
                    if kind == "GONE" and t <= end and job not in self.killed_jobs]

    def longest_phantom(self) -> tuple[float, tuple | None]:
        """Longest run of samples where lock counted units no executing job held while jobs waited."""
        longest, worst, start = 0.0, None, None
        for t, s1, lock, llen, _info, s2 in self.samples:
            phantom = lock - len(s1 | s2)
            if phantom > 0 and llen > 0:
                start = t if start is None else start
                if t - start > longest:
                    longest, worst = t - start, (round(start - self.t0, 2), round(t - self.t0, 2), phantom)
            else:
                start = None
        return longest, worst

    def describe(self) -> str:
        roles = ", ".join(f"{role}={pid}" for role, pid in self.pool_ids.items())
        faults = "\n".join(
            f"  {f['role']} {f['kind']} at {'+%ss' % f['at'] if f['at'] is not None else 'orphan end'}: {f['state']} "
            f"job={f.get('job_id')} command={f.get('command')} result={f.get('result')}"
            for f in self.faults
        )
        longest, worst = self.longest_phantom() if self.samples and hasattr(self, "t0") else (0.0, None)
        return (f"pools: {roles}\nfaults:\n{faults}\nlongest phantom run: {longest:.2f}s {worst}\n"
                f"timeline:\n" + "\n".join(self.timeline[-60:]))


def run_rig() -> Rig:
    rig = Rig()
    try:
        rig.execute()
    except EnvironmentDown as error:
        rig.environment_error = str(error)
    except Exception:  # noqa: BLE001 - every failure must still leave a scoreable rig
        rig.error = traceback.format_exc()
    finally:
        try:
            rig.teardown()
        except Exception:  # noqa: BLE001
            rig.notes.append(traceback.format_exc())
    return rig
