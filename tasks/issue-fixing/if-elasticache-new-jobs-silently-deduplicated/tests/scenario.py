"""Drives one run of the solution's dq against a fresh ElastiCache (Valkey) node and records what
it observed. tests/test_dedup.py grades the record; nothing here asserts.

Schedule (all ids fresh per run):
  phase 0  no worker alive. Owner deleted out of band, then re-added; persistent key reclaimed by
           a throttle add; an owner's id taken out of the wait list (record kept), then re-added;
           8-way concurrent adds on stale ids, arriving during a write pause.
  phase 1  pool of 5 workers. Throttle window outlives its finished and deleted owner. A hung
           owner's id taken out of the active list while its worker lives (probes), then its worker
           is killed and the id re-added. Keep-last: a running owner with kept data is deleted by
           hand and its worker killed, then a newer add arrives; a running owner's id is taken out
           of the active list, keep-last adds arrive, then it completes. Three hung owners claimed
           (one keep-last); duplicate probes every second.
  phase 2  the three holders SIGKILLed; all owners must come back through the stall checker.
  phase 3  two holders (simple, keep-last) SIGKILLed again: over the stall limit, their owners fail;
           the kept data must run; the simple id is re-added.
  phase 4  the other owner's handler raises once: backoff in delayed, probes continue.
  phase 5  the other owner completes; the id is re-added.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

import boto3
import redis
from botocore.config import Config

# Locks outlive a few seconds of the node (or the path to it) not answering: a live worker must
# never lose its lock to the venue, only to SIGKILL.
LOCK_MS = 8000
LOCK_RENEW_MS = 1000
STALLED_INTERVAL_MS = 1000
MAX_STALLED_COUNT = 1
ATTEMPTS = 3
BACKOFF_MS = 2000
POOL = 5
READY_DEADLINE_S = 15
CLAIM_DEADLINE_S = 10
KILL1_AFTER_S = 3
STALL_RECOVERY_DEADLINE_S = 16
KILL2_HOLD_S = 2
FAIL_AT_S = 30
LONG_JOB_S = 40
PROBE_INTERVAL_S = 1.0
THROTTLE_TTL_MS = 60000
RUN_DEADLINE_S = 10
RACE_IDS = 5
RACE_ADDS = 8
RACE_PAUSE_MS = 1500
DROPPED_HOLD_S = 3.5
KIT_ADDS = 3

ENV_MARKER = "/logs/verifier/environment_error.txt"
DQ_CMD = re.compile(r"(^|\s)-m\s+dq(\s|$)")


def now_ms():
    return int(time.time() * 1000)


class EnvironmentNotLive(RuntimeError):
    pass


def create_node():
    ec = boto3.client("elasticache", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    name = f"vh-{uuid.uuid4().hex[:10]}"
    try:
        ec.create_cache_cluster(CacheClusterId=name, Engine="valkey", CacheNodeType="cache.t3.micro",
                                NumCacheNodes=1)
        url = None
        for _ in range(300):
            c = ec.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)["CacheClusters"][0]
            if c["CacheClusterStatus"] == "available" and c.get("CacheNodes"):
                ep = c["CacheNodes"][0]["Endpoint"]
                url = f"redis://{ep['Address']}:{ep['Port']}/0"
                break
            time.sleep(1)
        if not url:
            raise EnvironmentNotLive(f"cache cluster {name} never became available")
        for _ in range(120):
            try:
                r = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
                r.ping()
                return url, r
            except redis.ConnectionError:
                time.sleep(0.5)
        raise EnvironmentNotLive(f"cache cluster {name} endpoint {url} not reachable")
    except EnvironmentNotLive:
        raise
    except Exception as exc:  # botocore / network: the emulator is not live
        raise EnvironmentNotLive(f"creating the cache cluster failed: {exc!r}")


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _proc_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _pids():
    return [int(p) for p in os.listdir("/proc") if p.isdigit()]


def _ancestors():
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def kill_leftover_solution_processes():
    """Processes the solver left running (workers, daemons) from its own session."""
    mine = _ancestors()
    for pid in _pids():
        if pid in mine:
            continue
        cmd = _proc_cmdline(pid)
        if DQ_CMD.search(cmd) or "/app/" in cmd:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class Harness:
    def __init__(self, url, r):
        tag = uuid.uuid4().hex[:8]
        self.url = url
        self.r = r
        self.queue = f"signup-{tag}"
        self.prefix = f"dq:{self.queue}:"
        self.runs_key = f"vh:{tag}:runs"
        self.gate_prefix = f"vh:{tag}:gate:"
        self.root = tempfile.mkdtemp(prefix="vh-")
        handler_dir = os.path.join(self.root, "handler")
        os.makedirs(handler_dir)
        shutil.copy(os.path.join(os.path.dirname(__file__), "harness", "vh_handler.py"), handler_dir)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
        self.env.update({"DQ_REDIS_URL": url, "PYTHONPATH": f"/app:{handler_dir}",
                         "PYTHONDONTWRITEBYTECODE": "1", "VH_RUNS_KEY": self.runs_key,
                         "VH_GATE_PREFIX": self.gate_prefix, "VH_QUEUE": self.queue})
        self.workers = {}        # pid -> Popen (session leader)
        self.kills = []          # {"pid", "ts", "target"}
        self.worker_logs = []
        self.lock = threading.Lock()
        self.job_deid = {}       # job id -> dedup id, from dq add results
        self.obs = {"errors": {}, "queue": self.queue}
        self.mine = _ancestors()

    # ---- solution processes --------------------------------------------------------------

    def _cwd(self):
        return tempfile.mkdtemp(dir=self.root, prefix="p-")

    def add(self, dedup_id, ttl_ms=None, name="welcome-email", data=None, rev=None):
        """rev given: a keep-last account sync carrying that revision."""
        if rev is not None:
            name, data = "account-sync", {"email": dedup_id, "rev": rev}
        cmd = [sys.executable, "-m", "dq", "add", "--queue", self.queue, "--name", name,
               "--data", json.dumps(data or {"email": dedup_id}), "--dedup-id", dedup_id]
        if ttl_ms is not None:
            cmd += ["--ttl-ms", str(ttl_ms)]
        if rev is not None:
            cmd += ["--keep-last"]
        t0 = now_ms()
        try:
            p = subprocess.run(cmd, cwd=self._cwd(), env=self.env, capture_output=True, text=True,
                               timeout=30)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err = "timeout", exc.stdout or "", exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
        return self._parse_add(dedup_id, t0, now_ms(), rc, out, err, rev)

    def _parse_add(self, dedup_id, t0, t1, rc, out, err, rev=None):
        res = {"dedup_id": dedup_id, "t0": t0, "t1": t1, "rc": rc, "job_id": None, "rev": rev,
               "deduplicated": None, "stderr": (err or "")[-800:], "stdout": (out or "")[-400:]}
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        if rc == 0 and lines:
            try:
                doc = json.loads(lines[-1])
                res["job_id"] = None if doc.get("job_id") is None else str(doc["job_id"])
                res["deduplicated"] = doc.get("deduplicated")
            except (ValueError, AttributeError):
                pass
        if res["job_id"] is not None and res["deduplicated"] is False:
            with self.lock:
                self.job_deid[res["job_id"]] = dedup_id
        return res

    def add_concurrently(self, dedup_id, n):
        procs = []
        for _ in range(n):
            cmd = [sys.executable, "-m", "dq", "add", "--queue", self.queue, "--name", "welcome-email",
                   "--data", json.dumps({"email": dedup_id}), "--dedup-id", dedup_id]
            procs.append((now_ms(), subprocess.Popen(cmd, cwd=self._cwd(), env=self.env, text=True,
                                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)))
        results = []
        for t0, p in procs:
            try:
                out, err = p.communicate(timeout=30)
                rc = p.returncode
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
                rc = "timeout"
            results.append(self._parse_add(dedup_id, t0, now_ms(), rc, out, err))
        return results

    def start_worker(self):
        cmd = [sys.executable, "-m", "dq", "worker", "--queue", self.queue, "--handler", "vh_handler:run",
               "--lock-ms", str(LOCK_MS), "--lock-renew-ms", str(LOCK_RENEW_MS),
               "--stalled-interval-ms", str(STALLED_INTERVAL_MS),
               "--max-stalled-count", str(MAX_STALLED_COUNT), "--attempts", str(ATTEMPTS),
               "--backoff-ms", str(BACKOFF_MS)]
        log = open(os.path.join(self.root, f"worker-{len(self.worker_logs)}.log"), "w")
        self.worker_logs.append(log.name)
        p = subprocess.Popen(cmd, cwd=self._cwd(), env=self.env, stdout=subprocess.PIPE, stderr=log,
                             text=True, start_new_session=True)
        ready = threading.Event()

        def pump():
            for line in p.stdout:
                if line.startswith("READY"):
                    ready.set()
                log.write("[stdout] " + line)
                log.flush()

        threading.Thread(target=pump, daemon=True).start()
        if not ready.wait(READY_DEADLINE_S):
            raise RuntimeError(f"worker {p.pid} did not print READY within {READY_DEADLINE_S}s "
                               f"(exit code {p.poll()})")
        self.workers[p.pid] = p
        return p.pid

    def start_workers(self, n):
        out, errs = [], []

        def one():
            try:
                out.append(self.start_worker())
            except Exception as exc:
                errs.append(repr(exc))

        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errs:
            raise RuntimeError("; ".join(errs))
        return out

    def _worker_of(self, pid):
        """The worker session a handler pid belongs to (the pid itself, or an ancestor)."""
        seen = 0
        while pid and pid > 1 and seen < 20:
            if pid in self.workers:
                return pid
            try:
                with open(f"/proc/{pid}/stat") as f:
                    pid = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                return None
            seen += 1
        return None

    def kill_holder(self, job_id, target):
        pid = self.latest_start(job_id)["pid"]
        leader = self._worker_of(pid)
        ts = now_ms()
        with self.lock:
            self.kills.append({"pid": pid, "ts": ts, "target": target, "leader": leader})
        if leader:
            try:
                os.killpg(leader, signal.SIGKILL)
            except OSError:
                pass
            self.workers.pop(leader).wait()
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return ts

    def stop_workers(self):
        for leader, p in list(self.workers.items()):
            try:
                os.killpg(leader, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        self.workers.clear()

    # ---- observations --------------------------------------------------------------------

    def runs(self):
        out = []
        for _id, f in self.r.xrange(self.runs_key):
            out.append({"ev": f.get("ev"), "job": f.get("job"), "deid": f.get("deid"),
                        "pid": int(f.get("pid", 0)), "attempt": f.get("attempt"), "rev": f.get("rev"),
                        "ts": int(f.get("ts", 0))})
        return out

    def starts(self, job_id):
        return [e for e in self.runs() if e["ev"] == "start" and e["job"] == job_id]

    def latest_start(self, job_id):
        s = self.starts(job_id)
        if not s:
            raise RuntimeError(f"job {job_id} has no start event")
        return s[-1]

    def wait_for(self, predicate, timeout_s, poll_s=0.05):
        deadline = time.time() + timeout_s
        while True:
            value = predicate()
            if value:
                return value
            if time.time() >= deadline:
                return value
            time.sleep(poll_s)

    def wait_rev_start(self, dedup_id, rev, timeout_s=RUN_DEADLINE_S):
        """The first run of dedup_id carrying revision rev (whichever job holds it)."""
        def got():
            es = [e for e in self.runs() if e["ev"] == "start" and e["deid"] == dedup_id
                  and e["rev"] == str(rev)]
            return es[0] if es else None
        return self.wait_for(got, timeout_s)

    def wait_event(self, job_id, ev, count=1, timeout_s=RUN_DEADLINE_S):
        def got():
            es = [e for e in self.runs() if e["ev"] == ev and e["job"] == job_id]
            return es[count - 1] if len(es) >= count else None
        return self.wait_for(got, timeout_s)

    def job_state(self, job_id):
        return self.r.hget(self.prefix + f"job:{job_id}", "state")

    def de(self, dedup_id):
        key = self.prefix + f"de:{dedup_id}"
        return {"value": self.r.get(key), "pttl": self.r.pttl(key)}

    def delete_job_out_of_band(self, job_id):
        """What on-call did with redis-cli: drop the job hash and its list/zset entries."""
        self.r.delete(self.prefix + f"job:{job_id}")
        for lst in ("wait", "active"):
            self.r.lrem(self.prefix + lst, 0, job_id)
        for zs in ("delayed", "completed", "failed"):
            self.r.zrem(self.prefix + zs, job_id)
        return now_ms()

    def scan_jobs(self):
        """job id -> {deid, state} for every job hash of the queue."""
        out = {}
        pat = re.compile(re.escape(self.prefix) + r"job:([^:]+)$")
        for key in self.r.scan_iter(match=self.prefix + "job:*", count=1000):
            m = pat.match(key)
            if not m or self.r.type(key) != "hash":
                continue
            h = self.r.hgetall(key)
            out[m.group(1)] = {"deid": h.get("deid"), "state": h.get("state")}
        return out

    def solution_processes_alive(self):
        alive = []
        for pid in _pids():
            if pid in self.workers or pid in self.mine:
                continue
            cmd = _proc_cmdline(pid)
            if DQ_CMD.search(cmd) or "/app/" in cmd or _proc_cwd(pid).startswith(self.root):
                alive.append({"pid": pid, "cmd": cmd[:200]})
        return alive


class Prober:
    """Adds a duplicate for one dedup id every PROBE_INTERVAL_S until stopped."""

    def __init__(self, h, dedup_id, owner):
        self.h, self.dedup_id, self.owner = h, dedup_id, owner
        self.results = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            started = time.time()
            res = self.h.add(self.dedup_id)
            res["expected_owner"] = self.owner
            res["owner_state_after"] = self.h.job_state(self.owner)
            self.results.append(res)
            self._stop.wait(max(0.0, PROBE_INTERVAL_S - (time.time() - started)))

    def stop(self):
        self._stop.set()
        self._t.join()
        return self.results


class KeepLastProber:
    """Keep-last adds for one id, a higher rev each time, every PROBE_INTERVAL_S while resumed.
    pause() returns once no add is in flight, so none can race the owner's crash."""

    def __init__(self, h, dedup_id, owner, rev=0):
        self.h, self.dedup_id, self.owner, self.rev = h, dedup_id, owner, rev
        self.results = []
        self._stop = threading.Event()
        self._on = threading.Event()
        self._on.set()
        self._busy = threading.Lock()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            if not self._on.wait(0.05):
                continue
            started = time.time()
            with self._busy:
                if self._stop.is_set() or not self._on.is_set():
                    continue
                self.rev += 1
                res = self.h.add(self.dedup_id, rev=self.rev)
                res["expected_owner"] = self.owner
                res["owner_state_after"] = self.h.job_state(self.owner)
                self.results.append(res)
            self._stop.wait(max(0.0, PROBE_INTERVAL_S - (time.time() - started)))

    def pause(self):
        self._on.clear()
        with self._busy:
            pass

    def resume(self):
        self._on.set()

    def stop(self):
        self._stop.set()
        self._on.set()
        self._t.join()
        return self.results


def fresh_email(tag):
    return f"{tag}.{uuid.uuid4().hex[:6]}@example.com"


def run():
    os.makedirs("/logs/verifier", exist_ok=True)
    try:
        url, r = create_node()
    except EnvironmentNotLive as exc:
        with open(ENV_MARKER, "w") as f:
            f.write(str(exc) + "\n")
        raise
    kill_leftover_solution_processes()
    h = Harness(url, r)
    obs = h.obs
    ids = {k: fresh_email(k) for k in ("ana", "ben", "cai", "dev", "eli", "fay", "gus", "kim", "kit",
                                       "lou")}
    obs["ids"] = ids

    def phase(name, fn):
        try:
            fn()
        except Exception:
            obs["errors"][name] = traceback.format_exc()[-3000:]

    # ---- phase 0: no worker alive ----------------------------------------------------------
    def phase0():
        p0 = obs["p0"] = {}
        d2 = ids["ben"]
        p0["first"] = h.add(d2)
        j2 = p0["first"]["job_id"]
        p0["dup"] = h.add(d2)
        p0["deleted_at"] = h.delete_job_out_of_band(j2)
        p0["readd"] = h.add(d2)
        p0["de_after"] = h.de(d2)
        new = p0["readd"]["job_id"]
        p0["readd_hash"] = h.r.hgetall(h.prefix + f"job:{new}") if new else {}
        p0["procs_after"] = h.solution_processes_alive()

        d5 = ids["eli"]
        p0["t_first"] = h.add(d5)
        h.delete_job_out_of_band(p0["t_first"]["job_id"])
        p0["t_readd"] = h.add(d5, ttl_ms=THROTTLE_TTL_MS)
        p0["t_de_after"] = h.de(d5)
        p0["procs_after_t"] = h.solution_processes_alive()

        # on-call takes an id out of the wait list and leaves its record
        d6 = ids["gus"]
        p0["g_first"] = h.add(d6)
        jg = p0["g_first"]["job_id"]
        p0["g_dup"] = h.add(d6)
        p0["g_dropped"] = h.r.lrem(h.prefix + "wait", 0, jg)
        p0["g_readd"] = h.add(d6)
        p0["g_procs"] = h.solution_processes_alive()

    phase("phase0", phase0)

    def race():
        rc = obs["race"] = []
        for i in range(RACE_IDS):
            rid = fresh_email(f"r{i}")
            first = h.add(rid)
            h.delete_job_out_of_band(first["job_id"])
            # The node holds every client for a moment (as in a failover or a slow command);
            # the adds queue up behind it and are released together.
            try:
                h.r.execute_command("CLIENT", "PAUSE", RACE_PAUSE_MS, "ALL")
                paused = True
            except redis.ResponseError:
                paused = False
            results = h.add_concurrently(rid, RACE_ADDS)
            rc.append({"id": rid, "first": first, "results": results, "de_after": h.de(rid),
                       "paused": paused})
        obs["procs_after_race"] = h.solution_processes_alive()

    phase("race", race)

    # ---- phase 1..5: worker pool -----------------------------------------------------------
    def pool():
        p = obs["pool"] = {}
        p["ready_at"] = now_ms()
        h.start_workers(POOL)
        p["ready_at"] = now_ms()

        readd = obs.get("p0", {}).get("readd", {}).get("job_id")
        if readd and obs["p0"]["readd"].get("deduplicated") is False:
            p["readd_end"] = h.wait_event(readd, "end")
        t_readd = obs.get("p0", {}).get("t_readd", {}).get("job_id")
        if t_readd and obs["p0"]["t_readd"].get("deduplicated") is False:
            p["t_readd_end"] = h.wait_event(t_readd, "end")
        g_readd = obs.get("p0", {}).get("g_readd", {}).get("job_id")
        if g_readd:
            # whatever the add answered, the job it names must run
            p["g_readd_end"] = h.wait_event(g_readd, "end")

        # throttle window outlives its owner
        d4 = ids["dev"]
        p["w_first"] = h.add(d4, ttl_ms=THROTTLE_TTL_MS)
        j4 = p["w_first"]["job_id"]
        p["w_end"] = h.wait_event(j4, "end")
        p["w_completed"] = h.wait_for(lambda: h.job_state(j4) == "completed", RUN_DEADLINE_S)
        p["w_after_finish"] = h.add(d4)
        h.delete_job_out_of_band(j4)
        time.sleep(1.0)
        p["w_after_delete"] = h.add(d4)
        p["w_after_delete_ttl"] = h.add(d4, ttl_ms=THROTTLE_TTL_MS)
        p["w_de"] = h.de(d4)

        # on-call takes a hung job's id out of the active list; its worker is still alive
        d7 = ids["fay"]
        h.r.set(h.gate_prefix + d7, "hang")
        p["f_first"] = h.add(d7)
        jf = p["f_first"]["job_id"]
        sf = h.wait_event(jf, "start", timeout_s=CLAIM_DEADLINE_S)
        p["f_claimed"] = bool(sf)
        if not sf:
            raise RuntimeError("owner was not claimed by the pool")
        p["f_dropped"] = h.r.lrem(h.prefix + "active", 0, jf)
        p["f_dropped_at"] = now_ms()
        probe_f = Prober(h, d7, jf)
        try:
            time.sleep(DROPPED_HOLD_S)
        finally:
            p["f_probes"] = probe_f.stop()
        p["f_kill"] = h.kill_holder(jf, "f")
        h.start_workers(1)
        lock_key = h.prefix + f"job:{jf}:lock"
        p["f_lock_gone"] = h.wait_for(lambda: not h.r.exists(lock_key), LOCK_MS / 1000 + 5)
        h.r.delete(h.gate_prefix + d7)
        p["f_readd"] = h.add(d7)
        jr = p["f_readd"]["job_id"]
        if jr:
            p["f_readd_end"] = h.wait_event(jr, "end")

        # keep-last: a running owner with kept data is deleted by hand and its worker killed;
        # a newer add arrives afterwards
        dl = ids["lou"]
        h.r.set(h.gate_prefix + dl, "hang")
        p["l_first"] = h.add(dl, rev=0)
        jl = p["l_first"]["job_id"]
        sl = h.wait_event(jl, "start", timeout_s=CLAIM_DEADLINE_S)
        p["l_claimed"] = bool(sl)
        if not sl:
            raise RuntimeError("keep-last owner was not claimed by the pool")
        p["l_kept"] = h.add(dl, rev=1)
        p["l_deleted_at"] = h.delete_job_out_of_band(jl)
        p["l_kill"] = h.kill_holder(jl, "l")
        h.start_workers(1)
        lock_key = h.prefix + f"job:{jl}:lock"
        p["l_lock_gone"] = h.wait_for(lambda: not h.r.exists(lock_key), LOCK_MS / 1000 + 5)
        h.r.delete(h.gate_prefix + dl)
        p["l_newer"] = h.add(dl, rev=2)
        p["l_newer_start"] = h.wait_rev_start(dl, 2)
        if p["l_newer_start"]:
            p["l_newer_end"] = h.wait_event(p["l_newer_start"]["job"], "end")

        # keep-last: a running owner's id is taken out of the active list (its worker lives),
        # keep-last adds arrive, then the owner completes
        dt = ids["kit"]
        h.r.set(h.gate_prefix + dt, "hang")
        p["t_first"] = h.add(dt, rev=0)
        jt = p["t_first"]["job_id"]
        st = h.wait_event(jt, "start", timeout_s=CLAIM_DEADLINE_S)
        p["t_claimed"] = bool(st)
        if not st:
            raise RuntimeError("keep-last owner was not claimed by the pool")
        p["t_dropped"] = h.r.lrem(h.prefix + "active", 0, jt)
        p["t_probes"] = []
        for rev in range(1, KIT_ADDS + 1):
            started = time.time()
            res = h.add(dt, rev=rev)
            res["expected_owner"] = jt
            res["owner_state_after"] = h.job_state(jt)
            p["t_probes"].append(res)
            time.sleep(max(0.0, PROBE_INTERVAL_S - (time.time() - started)))
        h.r.delete(h.gate_prefix + dt)
        p["t_end"] = h.wait_event(jt, "end")
        p["t_next_start"] = h.wait_rev_start(dt, KIT_ADDS)
        if p["t_next_start"]:
            p["t_next_end"] = h.wait_event(p["t_next_start"]["job"], "end")

        # three long owners: ana (simple, crashes, backoff, completes), cai (simple, over the stall
        # limit), kim (keep-last, over the stall limit with kept data)
        d1, d3, dk = ids["ana"], ids["cai"], ids["kim"]
        for d in (d1, d3, dk):
            h.r.set(h.gate_prefix + d, "hang")
        p["a_first"] = h.add(d1)
        p["c_first"] = h.add(d3)
        p["k_first"] = h.add(dk, rev=0)
        j1, j3, jk = p["a_first"]["job_id"], p["c_first"]["job_id"], p["k_first"]["job_id"]
        s1 = h.wait_event(j1, "start", timeout_s=CLAIM_DEADLINE_S)
        s3 = h.wait_event(j3, "start", timeout_s=CLAIM_DEADLINE_S)
        sk = h.wait_event(jk, "start", timeout_s=CLAIM_DEADLINE_S)
        p["a_claimed"], p["c_claimed"], p["k_claimed"] = bool(s1), bool(s3), bool(sk)
        if not (s1 and s3 and sk):
            raise RuntimeError("owners were not claimed by the pool")
        t1 = max(s1["ts"], s3["ts"], sk["ts"]) / 1000.0
        probe_a = Prober(h, d1, j1)
        probe_c = Prober(h, d3, j3)
        probe_k = KeepLastProber(h, dk, jk)
        try:
            # phase 2: crash the three holders
            time.sleep(max(0.0, t1 + KILL1_AFTER_S - time.time()))
            probe_k.pause()
            p["kill1_a"] = h.kill_holder(j1, "a")
            p["kill1_c"] = h.kill_holder(j3, "c")
            p["kill1_k"] = h.kill_holder(jk, "k")
            h.start_workers(3)
            p["a_restart"] = h.wait_event(j1, "start", count=2,
                                          timeout_s=STALL_RECOVERY_DEADLINE_S - (now_ms() - p["kill1_a"]) / 1000)
            p["c_restart"] = h.wait_event(j3, "start", count=2,
                                          timeout_s=STALL_RECOVERY_DEADLINE_S - (now_ms() - p["kill1_c"]) / 1000)
            p["k_restart"] = h.wait_event(jk, "start", count=2,
                                          timeout_s=STALL_RECOVERY_DEADLINE_S - (now_ms() - p["kill1_k"]) / 1000)
            if p["k_restart"]:
                probe_k.resume()

            # phase 3: crash cai's and kim's holders again, over the stall limit
            if p["c_restart"] and p["k_restart"]:
                back = max(p["c_restart"]["ts"], p["k_restart"]["ts"]) / 1000
                time.sleep(max(0.0, back + KILL2_HOLD_S - time.time()))
                p["c_probes"] = probe_c.stop()
                probe_k.pause()
                p["k_probes"] = probe_k.stop()
                p["kill2_c"] = h.kill_holder(j3, "c")
                p["kill2_k"] = h.kill_holder(jk, "k")
                h.r.delete(h.gate_prefix + dk)
                h.start_workers(2)
                p["c_failed"] = h.wait_for(lambda: h.job_state(j3) == "failed", STALL_RECOVERY_DEADLINE_S)
                p["k_failed"] = h.wait_for(lambda: h.job_state(jk) == "failed",
                                           STALL_RECOVERY_DEADLINE_S - (now_ms() - p["kill2_k"]) / 1000)
                p["k_failed_at"] = now_ms()
                h.r.delete(h.gate_prefix + d3)
                p["c_readd"] = h.add(d3)
                p["c_de_after"] = h.de(d3)
                jc2 = p["c_readd"]["job_id"]
                if jc2 and p["c_readd"]["deduplicated"] is False:
                    p["c_readd_end"] = h.wait_event(jc2, "end")
                last_rev = max([r["rev"] for r in p["k_probes"]] or [0])
                p["k_last_rev"] = last_rev
                p["k_next_start"] = h.wait_rev_start(dk, last_rev)
                if p["k_next_start"]:
                    p["k_next_end"] = h.wait_event(p["k_next_start"]["job"], "end")
            else:
                p["c_probes"] = probe_c.stop()
                p["k_probes"] = probe_k.stop()

            # phase 4: one failed attempt, backoff
            time.sleep(max(0.0, t1 + FAIL_AT_S - time.time()))
            p["fail_set_at"] = now_ms()
            h.r.set(h.gate_prefix + d1, "fail")
            p["a_error"] = h.wait_event(j1, "error")
            if p["a_error"]:
                p["a_retry_start"] = h.wait_event(j1, "start", count=len(h.starts(j1)) + 1,
                                                  timeout_s=BACKOFF_MS / 1000 + RUN_DEADLINE_S)

            # phase 5: the long owner completes
            time.sleep(max(0.0, t1 + LONG_JOB_S - time.time()))
        finally:
            p["a_probes"] = probe_a.stop()
            if "c_probes" not in p:
                p["c_probes"] = probe_c.stop()
            if "k_probes" not in p:
                p["k_probes"] = probe_k.stop()
        h.r.delete(h.gate_prefix + d1)
        h.r.delete(h.gate_prefix + dk)
        p["a_end"] = h.wait_event(j1, "end")
        p["a_completed"] = h.wait_for(lambda: h.job_state(j1) == "completed", RUN_DEADLINE_S)
        p["a_readd"] = h.add(d1)
        p["a_de_after"] = h.de(d1)
        ja2 = p["a_readd"]["job_id"]
        if ja2 and p["a_readd"]["deduplicated"] is False:
            p["a_readd_end"] = h.wait_event(ja2, "end")
        p["a_final_state"] = h.job_state(j1)
        p["c_final_state"] = h.job_state(j3)
        p["k_final_state"] = h.job_state(jk)

    phase("pool", pool)

    try:
        time.sleep(0.5)
        h.stop_workers()
        obs["runs"] = h.runs()
        obs["kills"] = h.kills
        obs["jobs"] = h.scan_jobs()
        obs["job_deid"] = dict(h.job_deid)
        obs["end_ts"] = now_ms()
    except Exception:
        obs["errors"]["collect"] = traceback.format_exc()[-3000:]
    try:
        tails = {}
        for path in h.worker_logs:
            with open(path, errors="replace") as f:
                tails[os.path.basename(path)] = f.read()[-1500:]
        obs["worker_logs"] = tails
    except Exception:
        pass
    with open("/logs/verifier/observations.json", "w") as f:
        json.dump(obs, f, indent=1, default=str)
    return obs
