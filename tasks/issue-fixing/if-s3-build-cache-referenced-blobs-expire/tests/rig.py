"""Staging rig for the verifier: S3 meter proxy, lifecycle janitor, scanner, driver, replicas.

Everything the grade reads comes from S3 itself (listed directly on the upstream endpoint, which
the replicas never see), from the meter's request log, and from the driver's own record of what
it sent and what it got back. Nothing is read from the replicas' code or files.
"""

import http.client
import json
import os
import random
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

UPSTREAM = os.environ.get("RIG_S3_UPSTREAM", "http://localhost:4566")
APP_DIR = os.environ.get("RIG_APP", "/app")
LOG_DIR = os.environ.get("RIG_LOGDIR", "/logs/verifier")
PYTHON = os.environ.get("RIG_PYTHON", "python3")

# Lifecycle and service settings (the staging values in docs/OPERATIONS.md).
AC_TTL = 20.0
CAS_TTL = 40.0
REFRESH_AGE = 7.0
MARGIN = CAS_TTL - AC_TTL
REAP_INTERVAL = 1.0
SCAN_INTERVAL = 0.5

# S3 behaviour injected by the meter.
COPY_DELAY = 2.0
HEAD_DELAY = 1.5
FAULT_AT = 20.0
FAULT_COUNT = 2

# Timeline.
R2_FETCH_AT = 2.0       # a build on r2 downloads X once (it stays on r2's disk)
HOT_STEPS = 23          # hot steps at t = 2k + 1, k = 1..HOT_STEPS
LEASE_STEPS = 22        # BwoB lease checks on r1 at t = 2k + 2, k = 1..LEASE_STEPS
BURST_AT = 8.0
BURST_N = 16
RESTART_AT = 20.0
PHASE1_END = 48.0
PHASE2_READS = list(range(12, 24))   # R2 GET /ac/A_k at t = 48 + (k - 12)
PROBE_AT = 50.0
PROBE_WAIT_MAX = 57.0   # probe no later than this, before the phase-2 reads end
RUN_MIN = 61.0
RECLAIM_SLACK = 3.0
CAS_RECLAIM = CAS_TTL + REFRESH_AGE + COPY_DELAY + RECLAIM_SLACK   # 52
AC_RECLAIM = AC_TTL + RECLAIM_SLACK                                 # 23
RUN_MAX = 59.0 + CAS_RECLAIM + 2.0
BLOB_SIZE = 4096
DISK_MAX_BYTES = 1 << 30
READY_TIMEOUT = 10.0


def _s3(endpoint=UPSTREAM):
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
        config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3},
                          max_pool_connections=16, connect_timeout=5, read_timeout=30))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cas_key(h):
    return f"cas.v2/{h[:2]}/{h}"


def ac_key(h):
    return f"ac/{h[:2]}/{h}"


def ts(dt):
    return dt.timestamp()


class Clock:
    def __init__(self):
        self.t0 = None

    def now(self):
        return time.time() - self.t0 if self.t0 is not None else -1000.0

    def rel(self, epoch):
        return epoch - self.t0


# ---------------------------------------------------------------------------------------------
# Meter: reverse proxy in front of S3. Logs every request, delays HEAD and COPY, throttles.
# ---------------------------------------------------------------------------------------------

_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
        "upgrade", "expect", "content-length"}

_SLOWDOWN = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>SlowDown</Code>'
             b'<Message>Please reduce your request rate.</Message>'
             b'<RequestId>RIGTHROTTLE</RequestId></Error>')


class Meter:
    """One listener per replica (`via`); listeners made with `share=` log and throttle together."""

    def __init__(self, clock, upstream=UPSTREAM, via="", share=None):
        self.clock = clock
        self.via = via
        u = urllib.parse.urlparse(upstream)
        self.up_host, self.up_port = u.hostname, u.port or 80
        self.lock = share.lock if share else threading.Lock()
        self.log = share.log if share else []
        self.faults = share.faults if share else {"key": None, "left": FAULT_COUNT}
        self.port = free_port()
        meter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                meter.handle(self)

            do_PUT = do_POST = do_HEAD = do_DELETE = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()

    @staticmethod
    def _read_body(h):
        if "chunked" in (h.headers.get("Transfer-Encoding") or "").lower():
            out = bytearray()
            while True:
                line = h.rfile.readline().strip()
                n = int(line.split(b";")[0], 16)
                if n == 0:
                    while h.rfile.readline().strip():
                        pass
                    break
                out += h.rfile.read(n)
                h.rfile.readline()
            return bytes(out)
        n = int(h.headers.get("Content-Length") or 0)
        return h.rfile.read(n) if n else None

    @staticmethod
    def _classify(method, path, copy_src):
        parsed = urllib.parse.urlsplit(path)
        parts = parsed.path.lstrip("/").split("/", 1)
        bucket = urllib.parse.unquote(parts[0]) if parts and parts[0] else ""
        key = urllib.parse.unquote(parts[1]) if len(parts) > 1 and parts[1] else ""
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if not key:
            op = "BUCKET_" + method
        elif method == "HEAD":
            op = "HEAD"
        elif method == "GET" and not (set(query) - {"versionId", "partNumber"}):
            op = "GET"
        elif method == "PUT" and copy_src:
            op = "COPY"
        elif method == "PUT" and not query:
            op = "PUT"
        else:
            op = method + ("?" + "&".join(sorted(query)) if query else "")
        src = ""
        if copy_src:
            src = urllib.parse.unquote(copy_src.split("?", 1)[0]).lstrip("/")
            src = src.split("/", 1)[1] if "/" in src else src
        return bucket, key, op, src

    def _should_fault(self, op, key, t):
        if op != "COPY" or key != self.faults["key"] or t < FAULT_AT:
            return False
        with self.lock:
            if self.faults["left"] > 0:
                self.faults["left"] -= 1
                return True
        return False

    def handle(self, h):
        t_in = self.clock.now()
        body = self._read_body(h)
        copy_src = h.headers.get("x-amz-copy-source")
        bucket, key, op, src = self._classify(h.command, h.path, copy_src)
        entry = {"t_in": t_in, "via": self.via, "method": h.command, "op": op, "bucket": bucket, "key": key,
                 "copy_source": src, "status": None, "t_fwd": None, "t_done": None}

        if self._should_fault(op, key, t_in):
            entry.update(status=503, t_done=self.clock.now(), fault=True)
            self._record(entry)
            self._send(h, 503, [("Content-Type", "application/xml")], _SLOWDOWN)
            return

        delay = COPY_DELAY if op == "COPY" else HEAD_DELAY if op == "HEAD" else 0.0
        if delay:
            time.sleep(delay)
        entry["t_fwd"] = self.clock.now()
        headers = {k: v for k, v in h.headers.items() if k.lower() not in _HOP}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            conn = http.client.HTTPConnection(self.up_host, self.up_port, timeout=60)
            conn.request(h.command, h.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = resp.getheaders()
            status = resp.status
            conn.close()
        except Exception as e:  # noqa: BLE001
            entry.update(status=599, t_done=self.clock.now(), error=str(e))
            self._record(entry)
            self._send(h, 502, [], b"")
            return
        entry.update(status=status, t_done=self.clock.now())
        self._record(entry)
        out = [(k, v) for k, v in resp_headers
               if k.lower() not in _HOP and k.lower() not in ("date", "server")]
        if h.command == "HEAD":
            cl = dict((k.lower(), v) for k, v in resp_headers).get("content-length", "0")
            self._send(h, status, out, b"", head_length=cl)
        else:
            self._send(h, status, out, data)

    def _record(self, entry):
        with self.lock:
            self.log.append(entry)

    @staticmethod
    def _send(h, status, headers, data, head_length=None):
        try:
            h.send_response(status)
            for k, v in headers:
                h.send_header(k, v)
            h.send_header("Content-Length", head_length if head_length is not None
                          else str(len(data)))
            h.end_headers()
            if data and h.command != "HEAD":
                h.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------------------------
# Janitor (lifecycle) and scanner (observations of the bucket).
# ---------------------------------------------------------------------------------------------

def list_prefix(client, bucket, prefix):
    out = {}
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        for o in resp.get("Contents", []):
            out[o["Key"]] = ts(o["LastModified"])
        if not resp.get("IsTruncated"):
            return out
        token = resp["NextContinuationToken"]


class Janitor(threading.Thread):
    def __init__(self, clock, bucket, stop):
        super().__init__(daemon=True)
        self.clock, self.bucket, self.stop_ev = clock, bucket, stop
        self.client = _s3()
        self.deleted = {}   # key -> list of (t, lm_rel)
        self.errors = []

    def run(self):
        while not self.stop_ev.is_set():
            start = time.time()
            try:
                for prefix, ttl in (("ac/", AC_TTL), ("cas.v2/", CAS_TTL)):
                    for key, lm in list_prefix(self.client, self.bucket, prefix).items():
                        if time.time() - lm > ttl:
                            try:
                                self.client.delete_object(Bucket=self.bucket, Key=key)
                                self.deleted.setdefault(key, []).append(
                                    (self.clock.now(), self.clock.rel(lm)))
                            except ClientError as e:
                                self.errors.append((self.clock.now(), key, str(e)))
            except Exception as e:  # noqa: BLE001
                self.errors.append((self.clock.now(), "", repr(e)))
            self.stop_ev.wait(max(0.0, REAP_INTERVAL - (time.time() - start)))


class Scanner(threading.Thread):
    def __init__(self, clock, bucket, stop):
        super().__init__(daemon=True)
        self.clock, self.bucket, self.stop_ev = clock, bucket, stop
        self.client = _s3()
        self.history = {}    # key -> {"lms": {lm_rel: first_seen}, "first_seen", "last_seen"}
        self.bodies = {}     # ac key -> list of cas keys
        self.dangling = []
        self.errors = []
        self.scans = 0
        self.lm_whole_seconds = True

    def _observe(self, listing, t):
        for key, lm in listing.items():
            rec = self.history.setdefault(key, {"lms": {}, "first_seen": t, "last_seen": t})
            rec["last_seen"] = t
            if lm % 1.0:
                self.lm_whole_seconds = False
            rec["lms"].setdefault(round(self.clock.rel(lm), 3), t)

    def _outputs(self, key):
        if key not in self.bodies:
            try:
                body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                outs = json.loads(body).get("outputs", [])
                self.bodies[key] = [cas_key(d["hash"]) for d in outs]
            except ClientError:
                return None
            except Exception:  # noqa: BLE001
                self.bodies[key] = []
        return self.bodies[key]

    def _exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def run(self):
        while not self.stop_ev.is_set():
            start = time.time()
            try:
                t_cas = self.clock.now()
                cas = list_prefix(self.client, self.bucket, "cas.v2/")
                self._observe(cas, t_cas)
                t_ac = self.clock.now()
                ac = list_prefix(self.client, self.bucket, "ac/")
                self._observe(ac, t_ac)
                self.scans += 1
                for key, lm in ac.items():
                    outs = self._outputs(key) or []
                    for ck in outs:
                        if ck in cas or self.clock.rel(lm) >= t_cas - 1.0:
                            continue
                        # Listed absent while the AC entry already existed; confirm directly.
                        t_check = self.clock.now()
                        if not self._exists(ck) and self._exists(key):
                            self.dangling.append({"t": round(t_check, 2), "ac": key, "cas": ck,
                                                  "ac_lm": round(self.clock.rel(lm), 2)})
            except Exception as e:  # noqa: BLE001
                self.errors.append((self.clock.now(), repr(e)))
            self.stop_ev.wait(max(0.0, SCAN_INTERVAL - (time.time() - start)))


# ---------------------------------------------------------------------------------------------
# Replicas and the driver.
# ---------------------------------------------------------------------------------------------

class Replica:
    def __init__(self, role, meter_url, bucket, workdir):
        self.role = role
        self.port = free_port()
        self.disk_dir = os.path.join(workdir, f"disk-{role}")
        os.makedirs(self.disk_dir, exist_ok=True)
        self.log_path = os.path.join(LOG_DIR, f"replica-{role}.log")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_ENDPOINT_URL")
               and not k.startswith("RIG_") and k not in ("PYTEST_CURRENT_TEST",)}
        env.update({
            "ROLE": role,
            "LISTEN": f"127.0.0.1:{self.port}",
            "S3_ENDPOINT": meter_url,
            "AWS_ENDPOINT_URL": meter_url,
            "AWS_ENDPOINT_URL_S3": meter_url,
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
            "BUCKET": bucket,
            "DISK_DIR": self.disk_dir,
            "DISK_MAX_BYTES": str(DISK_MAX_BYTES),
            "CAS_REFRESH_AGE": str(int(REFRESH_AGE)),
            "PYTHONPATH": APP_DIR,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.env = env
        self.proc = None
        self.starts = []

    def start(self):
        cwd = tempfile.mkdtemp(prefix=f"run-{self.role}-")
        logf = open(self.log_path, "ab")
        logf.write(f"\n==== start {time.strftime('%H:%M:%S')} ====\n".encode())
        logf.flush()
        self.proc = subprocess.Popen([PYTHON, "-m", "cachesvc"], cwd=cwd, env=self.env,
                                     stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        logf.close()

    def healthy(self):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
            conn.request("GET", "/healthz")
            ok = conn.getresponse().status == 200
            conn.close()
            return ok
        except OSError:
            return False

    def wait_ready(self, timeout=READY_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            if self.healthy():
                return True
            time.sleep(0.05)
        return False

    def stop(self, grace=3.0):
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(5)


class Driver:
    """The gateway: sends requests, retries a replica that is down, records everything."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.events = []

    def call(self, replica, method, path, body=None, names=(), tag="", retry_for=6.0):
        deadline = time.time() + retry_for
        attempts = 0
        while True:
            attempts += 1
            t_start = self.clock.now()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", replica.port, timeout=30)
                if isinstance(body, bytes):
                    payload, ctype = body, "application/octet-stream"
                else:
                    payload = None if body is None else json.dumps(body).encode()
                    ctype = "application/json"
                headers = {"Content-Type": ctype}
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
                conn.close()
                break
            except (ConnectionRefusedError, ConnectionResetError, http.client.RemoteDisconnected,
                    BrokenPipeError) as e:
                if time.time() >= deadline:
                    status, data = 0, repr(e).encode()
                    break
                time.sleep(0.1)
            except OSError as e:
                status, data = 0, repr(e).encode()
                break
        t_end = self.clock.now()
        ev = {"tag": tag, "replica": replica.role, "method": method, "path": path,
              "status": status, "t_start": round(t_start, 3), "t_end": round(t_end, 3),
              "latency": round(t_end - t_start, 3), "attempts": attempts,
              "names": list(names)}
        try:
            ev["reply"] = json.loads(data) if data and path != "/healthz" and \
                not path.startswith("/cas/") else None
        except ValueError:
            ev["reply"] = None
        with self.lock:
            self.events.append(ev)
        return ev


def run_scenario():
    os.makedirs(LOG_DIR, exist_ok=True)
    clock = Clock()
    rng = random.Random()
    bucket = "bazel-cache-" + uuid.uuid4().hex[:10]
    admin = _s3()
    deploy(boto3.client("cloudformation", endpoint_url=UPSTREAM, region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test"),
           "cache-seed-" + bucket,
           Path(__file__).parent / "cdk_seed/seed.template.json", BucketName=bucket)

    x_data, y_data = os.urandom(BLOB_SIZE), os.urandom(BLOB_SIZE)
    import hashlib
    X = hashlib.sha256(x_data).hexdigest()
    Y = hashlib.sha256(y_data).hexdigest()
    A = [hashlib.sha256(f"action-{rng.random()}-{k}".encode()).hexdigest()
         for k in range(HOT_STEPS + 1)]
    B = hashlib.sha256(f"action-b-{rng.random()}".encode()).hexdigest()
    dx = {"hash": X, "size": BLOB_SIZE}
    dy = {"hash": Y, "size": BLOB_SIZE}

    meter = Meter(clock, via="r1")
    meter2 = Meter(clock, via="r2", share=meter)
    meter.faults["key"] = cas_key(X)
    meter.start()
    meter2.start()
    workdir = tempfile.mkdtemp(prefix="cachesvc-rig-")
    r1 = Replica("r1", meter.url, bucket, workdir)
    r2 = Replica("r2", meter2.url, bucket, workdir)
    driver = Driver(clock)
    stop = threading.Event()
    janitor = Janitor(clock, bucket, stop)
    scanner = Scanner(clock, bucket, stop)
    checks = []        # direct S3 observations made by the driver right after acks
    restarts = []
    result = {"bucket": bucket, "X": X, "Y": Y, "A": A, "B": B, "events": driver.events,
              "checks": checks, "restarts": restarts, "setup_error": None}

    def s3_has(key, expect_body=None):
        try:
            obj = admin.get_object(Bucket=bucket, Key=key)
            if expect_body is None:
                return True
            return json.loads(obj["Body"].read()) == expect_body
        except ClientError:
            return False
        except ValueError:
            return False

    def put_cas(rep, h, data, names, tag):
        ev = driver.call(rep, "PUT", f"/cas/{h}", data, names=names, tag=tag)
        checks.append({"t": clock.now(), "what": "cas_after_ack", "key": cas_key(h),
                       "ack": ev["status"], "present": ev["status"] == 200 and s3_has(cas_key(h))})
        return ev

    def put_ac(rep, h, outputs, names, tag):
        body = {"outputs": outputs}
        ev = driver.call(rep, "PUT", f"/ac/{h}", body, names=names, tag=tag)
        checks.append({"t": clock.now(), "what": "ac_after_ack", "key": ac_key(h),
                       "ack": ev["status"],
                       "present": ev["status"] == 200 and s3_has(ac_key(h), body)})
        return ev

    def missing_hashes(ev):
        try:
            return {d["hash"] for d in ev["reply"]["missing"]}
        except (TypeError, KeyError):
            return None

    def initial():
        ev = driver.call(r1, "POST", "/findmissing", {"digests": [dx, dy]}, names=(X, Y),
                         tag="t0_findmissing")
        miss = missing_hashes(ev) or {X, Y}
        if X in miss:
            put_cas(r1, X, x_data, (X,), "t0_put_cas")
        if Y in miss:
            put_cas(r1, Y, y_data, (Y,), "t0_put_cas")
        put_ac(r1, A[0], [dx], (X,), "put_ac")
        put_ac(r1, B, [dy], (Y,), "put_ac")

    last_hot_acked = threading.Event()   # the probe reads A[HOT_STEPS] only once it is written

    def hot_step(k):
        try:
            ev = driver.call(r1, "POST", "/findmissing", {"digests": [dx]}, names=(X,), tag="hot")
            miss = missing_hashes(ev)
            if miss is None or X in miss:
                put_cas(r1, X, x_data, (X,), "hot_put_cas")
            put_ac(r1, A[k], [dx], (X,), "put_ac")
        finally:
            if k == HOT_STEPS:
                last_hot_acked.t = clock.now()
                last_hot_acked.set()

    def lease_step(k):
        # A build without the bytes that got A_{k-1} as a hit checks that its output is still
        # there. It holds X only remotely, so it uploads nothing whatever the reply says.
        ev = driver.call(r1, "GET", f"/ac/{A[k - 1]}", tag="lease_get_ac")
        if ev["status"] == 200 and isinstance(ev.get("reply"), dict):
            ev["names"] = [d.get("hash") for d in ev["reply"].get("outputs", [])
                           if isinstance(d, dict)]
        driver.call(r1, "POST", "/findmissing", {"digests": [dx]}, names=(X,), tag="lease")

    def burst_one():
        driver.call(r1, "POST", "/findmissing", {"digests": [dx]}, names=(X,), tag="burst")

    def restart_r1():
        t_kill = clock.now()
        r1.stop()
        t_down = clock.now()
        r1.start()
        ok = r1.wait_ready()
        restarts.append({"t_kill": round(t_kill, 3), "t_down": round(t_down, 3),
                         "t_ready": round(clock.now(), 3), "ready": ok})

    def r2_fetch():
        # A build pinned to r2 needs libcore.a's bytes; r2 fetches it from the bucket and keeps
        # it on disk. Nothing on r2 asks about X again until phase 2.
        driver.call(r2, "GET", f"/cas/{X}", names=(X,), tag="r2_fetch")

    def phase2_read(k):
        ev = driver.call(r2, "GET", f"/ac/{A[k]}", tag="p2_get_ac")
        ev["ac_index"] = k
        if ev["status"] == 200 and isinstance(ev.get("reply"), dict):
            ev["names"] = [d.get("hash") for d in ev["reply"].get("outputs", [])
                           if isinstance(d, dict)]
        # The builds served from r2 hold X only remotely and check it on r2 (lease extension).
        driver.call(r2, "POST", "/findmissing", {"digests": [dx]}, names=(X,), tag="lease")

    def probe():
        # Probe the entry's output, not the latency of its write: if the last hot write is still
        # in flight at PROBE_AT, read half a second after its ack (giving up at PROBE_WAIT_MAX).
        if last_hot_acked.wait(max(0.0, PROBE_WAIT_MAX - clock.now())):
            time.sleep(max(0.0, last_hot_acked.t + 0.5 - clock.now()))
        ev = driver.call(r2, "GET", f"/ac/{A[HOT_STEPS]}", tag="probe")
        if ev["status"] == 200 and isinstance(ev.get("reply"), dict):
            ev["names"] = [d.get("hash") for d in ev["reply"].get("outputs", [])
                           if isinstance(d, dict)]
        result["probe"] = {"status": ev["status"], "reply": ev.get("reply"),
                           "cas_present": s3_has(cas_key(X)), "t": clock.now()}

    schedule = [(0.0, initial, ()), (R2_FETCH_AT, r2_fetch, ())]
    schedule += [(2.0 * k + 1.0, hot_step, (k,)) for k in range(1, HOT_STEPS + 1)]
    schedule += [(2.0 * k + 2.0, lease_step, (k,)) for k in range(1, LEASE_STEPS + 1)]
    schedule += [(BURST_AT, burst_one, ()) for _ in range(BURST_N)]
    schedule += [(RESTART_AT, restart_r1, ())]
    schedule += [(PHASE1_END + (k - PHASE2_READS[0]), phase2_read, (k,)) for k in PHASE2_READS]
    schedule += [(PROBE_AT, probe, ())]

    threads = []
    try:
        r1.start()
        r2.start()
        ok1, ok2 = r1.wait_ready(), r2.wait_ready()
        result["ready"] = [ok1, ok2]
        if not (ok1 and ok2):
            result["setup_error"] = f"replicas not healthy within {READY_TIMEOUT}s: r1={ok1} r2={ok2}"
            return result

        clock.t0 = time.time() + 0.5
        janitor.start()
        scanner.start()

        def runner(at, fn, args):
            delay = clock.t0 + at - time.time()
            if delay > 0:
                time.sleep(delay)
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                result.setdefault("driver_errors", []).append(f"{fn.__name__}{args}: {e!r}")

        for at, fn, args in schedule:
            t = threading.Thread(target=runner, args=(at, fn, args), daemon=True)
            t.start()
            threads.append(t)

        keys = [ac_key(a) for a in A] + [ac_key(B), cas_key(X), cas_key(Y)]
        while True:
            time.sleep(0.5)
            t = clock.now()
            if t >= RUN_MAX:
                break
            if t >= RUN_MIN and scanner.scans > 0 and all(
                    k in scanner.history and scanner.history[k]["last_seen"] < t - 1.5
                    for k in keys):
                break
        for th in threads:
            th.join(timeout=35)
    finally:
        stop.set()
        r1.stop()
        r2.stop()
        meter.stop()
        meter2.stop()
        janitor.join(timeout=10)
        scanner.join(timeout=10)

    result.update({
        "t_end": clock.now(),
        "meter": meter.log,
        "history": scanner.history,
        "lm_whole_seconds": scanner.lm_whole_seconds,
        "dangling": scanner.dangling,
        "scanner_errors": scanner.errors,
        "janitor_deleted": janitor.deleted,
        "janitor_errors": janitor.errors,
    })
    with open(os.path.join(LOG_DIR, "run.json"), "w") as f:
        json.dump(result, f, indent=1, default=str)
    return result


def preflight():
    """Return None if the S3 endpoint behaves as the rig needs, else a reason."""
    try:
        c = _s3()
        bucket = "rig-preflight-" + uuid.uuid4().hex[:8]
        c.create_bucket(Bucket=bucket)
        c.put_object(Bucket=bucket, Key="k", Body=b"x")
        lm0 = c.head_object(Bucket=bucket, Key="k")["LastModified"]
        time.sleep(1.2)
        c.copy_object(Bucket=bucket, Key="k", CopySource={"Bucket": bucket, "Key": "k"},
                      MetadataDirective="REPLACE")
        lm1 = c.head_object(Bucket=bucket, Key="k")["LastModified"]
        listed = c.list_objects_v2(Bucket=bucket)["Contents"][0]["LastModified"]
        c.delete_object(Bucket=bucket, Key="k")
        gone = "Contents" not in c.list_objects_v2(Bucket=bucket)
        c.delete_bucket(Bucket=bucket)
        if not lm1 > lm0 or not listed > lm0:
            return f"copy onto itself did not move LastModified ({lm0} -> {lm1}, listed {listed})"
        if not gone:
            return "DeleteObject did not remove the object"
        return None
    except Exception as e:  # noqa: BLE001
        return f"S3 endpoint {UPSTREAM} unusable: {e!r}"
