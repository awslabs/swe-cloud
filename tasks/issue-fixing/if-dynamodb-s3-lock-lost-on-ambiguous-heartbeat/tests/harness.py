"""Verifier harness: per-worker fault proxy, trial driver, and audits.

Each worker gets its own HTTP proxy in front of Floci (its --endpoint). The
proxy forwards, holds, drops or blackholes requests according to the rules the
trial arms, and logs every request with monotonic timestamps. Ground truth is
read directly from Floci, never through a proxy.
"""

import hashlib
import json
import os
import random
import re
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import http.client
import uuid
from pathlib import Path

from cdk_seed.cfn_deploy import deploy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

UPSTREAM = os.environ.get("FLOCI_ENDPOINT", "http://localhost:4566")
APP_DIR = os.environ.get("FW_APP_DIR", "/app")
PYTHON = os.environ.get("FW_PYTHON", "python3")
REGION = "us-east-1"

LEASE_MS = 6000
HEARTBEAT_MS = 1000
OBSERVER_REFRESH_MS = 500
TAKEOVER_BUFFER_MS = 1000
WORK_INTERVAL_MS = 500
EARLY_TAKEOVER_TOLERANCE_MS = 200
TAKEOVER_DEADLINE_MS = 9500
WARMUP_ENTRIES = 3
A_OBSERVE_MS = 18000
A_MIN_ENTRIES_AFTER_FAULT = 10
B_HOLD_MAX_MS = 30000
B_POST_RELEASE_MS = 3000
C_RESOLVE_DELAY_MS = 3500
C_OBSERVE_MS = 15000
HANDOVER_MS = 5000
R_WAIT_MS = 15000
R_SETTLE_MS = 2500
CAPTURE_TIMEOUT_MS = 5000
READY_TIMEOUT_MS = 20000
PROXY_BASE_PORT = int(os.environ.get("FW_PROXY_BASE_PORT", "4600"))
LOCK_KEY = "ledger-lock"
LEDGER_KEY = "ledger.json"

DDB_READ_OPS = {"GetItem", "Query", "Scan", "BatchGetItem", "TransactGetItems"}
DDB_WRITE_OPS = {"UpdateItem", "PutItem", "DeleteItem"}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "expect", "proxy-connection",
               "te", "trailer", "upgrade", "content-length"}


def aws(service, **extra):
    cfg = Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=5, read_timeout=20,
                 **extra)
    return boto3.client(service, endpoint_url=UPSTREAM, region_name=REGION,
                        aws_access_key_id="test", aws_secret_access_key="test", config=cfg)


def ddb_tables_in(op, body):
    names = set()
    if not isinstance(body, dict):
        return names
    if "TableName" in body:
        names.add(body["TableName"])
    for t in (body.get("RequestItems") or {}):
        names.add(t)
    for item in body.get("TransactItems") or []:
        for part in item.values():
            if isinstance(part, dict) and "TableName" in part:
                names.add(part["TableName"])
    return names


def _strings(value, out):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "S" and isinstance(v, str):
                out.add(v)
            else:
                _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)
    return out


def condition_refs_rvn_value(body, rvn):
    """(has_rvn_clause, rvn_in_condition_values) for an UpdateItem/PutItem body."""
    cond = body.get("ConditionExpression") or ""
    names = body.get("ExpressionAttributeNames") or {}
    values = body.get("ExpressionAttributeValues") or {}
    rvn_names = {k for k, v in names.items() if v == "recordVersionNumber"}
    has_clause = "recordVersionNumber" in cond or any(
        re.search(re.escape(n) + r"(?![A-Za-z0-9_])", cond) for n in rvn_names)
    used = set(re.findall(r":[A-Za-z0-9_]+", cond))
    cond_values = set()
    for placeholder in used:
        if placeholder in values:
            _strings(values[placeholder], cond_values)
    return has_clause, rvn in cond_values


# --------------------------------------------------------------------------- proxy


class Proxy:
    """Fault-injecting HTTP/1.1 proxy for one worker."""

    def __init__(self, worker, port, table, bucket, t0):
        self.worker = worker
        self.port = port
        self.table = table
        self.bucket = bucket
        self.t0 = t0
        self.mu = threading.Lock()
        self.log = []
        self.stop = threading.Event()
        self.events = {}
        # rules
        self.cut = False
        self.ddb_blackhole = False
        self.drop_next_update = None      # None | "reset" | "500"
        self.drop_ops = ("UpdateItem",)   # lock-table writes the drop rule applies to
        self.after_drop = None            # None | "c_mode"
        self.c_mode_s = None
        self.capture_put = False
        self.capture_get = False
        self.held_puts = []
        self.release_puts = threading.Event()
        self.held_get = None
        self.release_get = threading.Event()
        self.get_delivered = threading.Event()
        self.on_capture = None            # callback(kind)
        self.fired = threading.Event()
        self.fire_info = {}
        self.sockets = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler_class())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def now(self):
        return time.monotonic() - self.t0

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        self.release_puts.set()
        self.release_get.set()
        try:
            self.server.shutdown()
        except Exception:
            pass
        self.server.server_close()
        with self.mu:
            socks = list(self.sockets)
        for s in socks:
            _reset_socket(s)

    def records(self):
        with self.mu:
            return [dict(r) for r in self.log]

    # -- request classification and rules ----------------------------------

    def classify(self, method, path, headers, body):
        rec = {"worker": self.worker, "t_recv": self.now(), "method": method, "path": path,
               "kind": "other", "op": None, "lock_table": False, "ledger": False,
               "action": None, "t_fwd": None, "t_resp": None, "status": None, "etag": None}
        target = headers.get("X-Amz-Target") or headers.get("x-amz-target") or ""
        if target.startswith("DynamoDB_20120810."):
            rec["kind"] = "ddb"
            rec["op"] = target.split(".", 1)[1]
            try:
                parsed = json.loads(body.decode() or "{}")
            except Exception:
                parsed = {}
            rec["body"] = parsed
            rec["lock_table"] = self.table in ddb_tables_in(rec["op"], parsed)
        elif target.startswith("DynamoDBStreams"):
            rec["kind"] = "streams"
        else:
            split = urlsplit(path)
            p = split.path
            host = (headers.get("Host") or "").split(":")[0]
            if host.startswith(self.bucket + "."):
                p = "/" + self.bucket + p
            rec["kind"] = "s3"
            rec["op"] = method
            rec["ledger"] = (p == "/%s/%s" % (self.bucket, LEDGER_KEY))
            rec["query"] = split.query
            if method == "PUT" and rec["ledger"]:
                rec["body_md5"] = hashlib.md5(body).hexdigest()
                rec["if_match"] = headers.get("If-Match")
        return rec

    def decide(self, rec):
        """Return the action for this request (under self.mu)."""
        if self.cut or self.stop.is_set():
            return "blackhole"
        if rec["kind"] == "ddb":
            if rec["op"] in ("TransactWriteItems", "BatchWriteItem") and rec["lock_table"]:
                return "reject"
            if self.ddb_blackhole:
                return "blackhole"
            if self.c_mode_s is not None and rec["lock_table"]:
                if rec["op"] in DDB_WRITE_OPS:
                    return "blackhole"
                if rec["op"] in DDB_READ_OPS:
                    if rec["t_recv"] < self.c_mode_s + C_RESOLVE_DELAY_MS / 1000.0:
                        return "blackhole"
                    return "forward"
            if self.drop_next_update and rec["op"] in self.drop_ops and rec["lock_table"]:
                return "drop:" + self.drop_next_update
            return "forward"
        if rec["kind"] == "s3" and rec["ledger"] and not rec.get("query", "").startswith("versions"):
            if rec["op"] == "PUT" and self.capture_put:
                return "hold_put"
            if rec["op"] == "GET" and self.capture_get and "versionId" not in rec.get("query", ""):
                self.capture_get = False
                return "hold_get"
        return "forward"

    def _handler_class(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                with proxy.mu:
                    proxy.sockets.add(self.connection)
                self.upstream = None

            def finish(self):
                try:
                    super().finish()
                except Exception:
                    pass
                with proxy.mu:
                    proxy.sockets.discard(self.connection)
                if self.upstream is not None:
                    try:
                        self.upstream.close()
                    except Exception:
                        pass

            def _read_body(self):
                te = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in te:
                    chunks = []
                    while True:
                        line = self.rfile.readline()
                        size = int(line.split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            break
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                    return b"".join(chunks)
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _forward(self, body):
                u = urlsplit(UPSTREAM)
                if self.upstream is None:
                    self.upstream = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)
                conn = self.upstream
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in self.headers.items():
                    if k.lower() in HOP_HEADERS:
                        continue
                    conn.putheader(k, v)
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                resp = conn.getresponse()
                data = resp.read()
                headers = resp.getheaders()
                status = resp.status
                if status == 412 or resp.will_close:
                    # Floci poisons a keep-alive connection after a rejected
                    # conditional write; never reuse it.
                    conn.close()
                    self.upstream = None
                return status, resp.reason, headers, data

            def _write_response(self, status, reason, headers, data):
                out = ["HTTP/1.1 %d %s" % (status, reason or "")]
                for k, v in headers:
                    if k.lower() in HOP_HEADERS:
                        continue
                    out.append("%s: %s" % (k, v))
                if self.command == "HEAD":
                    clen = [v for k, v in headers if k.lower() == "content-length"]
                    out.append("Content-Length: %s" % (clen[0] if clen else "0"))
                    payload = b""
                else:
                    out.append("Content-Length: %d" % len(data))
                    payload = data
                raw = ("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + payload
                self.wfile.write(raw)
                self.wfile.flush()

            def _reset(self):
                self.close_connection = True
                _reset_socket(self.connection)

            def _blackhole(self):
                self.close_connection = True
                while not proxy.stop.wait(0.2):
                    pass
                _reset_socket(self.connection)

            def _handle(self):
                body = self._read_body()
                rec = proxy.classify(self.command, self.path, self.headers, body)
                with proxy.mu:
                    action = proxy.decide(rec)
                    rec["action"] = action
                    proxy.log.append(rec)
                if action == "blackhole":
                    self._blackhole()
                    return
                if action == "reject":
                    payload = json.dumps({"__type": "com.amazon.coral.validate#ValidationException",
                                          "message": "lock table accepts UpdateItem heartbeats only"}).encode()
                    rec["status"] = 400
                    self._write_response(400, "Bad Request",
                                         [("Content-Type", "application/x-amz-json-1.0")], payload)
                    return
                if action == "hold_put":
                    first = False
                    with proxy.mu:
                        proxy.held_puts.append(rec)
                        if not proxy.ddb_blackhole:
                            proxy.ddb_blackhole = True
                            first = True
                    if first:
                        proxy._fire("capture_put", rec)
                    proxy.release_puts.wait()
                    if proxy.stop.is_set():
                        self._reset()
                        return
                    self._forward_and_relay(rec, body, ordered=True)
                    return
                if action == "hold_get":
                    proxy._fire("capture_get", rec)
                    proxy.release_get.wait()
                    if proxy.stop.is_set():
                        self._reset()
                        return
                    rec["t_fwd"] = proxy.now()
                    status, reason, headers, data = self._forward(body)
                    rec["t_resp"] = proxy.now()
                    rec["status"] = status
                    try:
                        self._write_response(status, reason, headers, data)
                    except OSError:
                        rec["client_gone"] = True
                    proxy.get_delivered.set()
                    return
                if action.startswith("drop:"):
                    rec["t_fwd"] = proxy.now()
                    status, reason, headers, data = self._forward(body)
                    rec["t_resp"] = proxy.now()
                    rec["status"] = status
                    fire = False
                    with proxy.mu:
                        if status == 200 and proxy.drop_next_update:
                            proxy.drop_next_update = None
                            fire = True
                            if proxy.after_drop == "c_mode":
                                proxy.c_mode_s = rec["t_fwd"]
                    if not fire:
                        self._write_response(status, reason, headers, data)
                        return
                    rec["dropped"] = action[5:]
                    proxy._fire("dropped", rec)
                    if action == "drop:reset":
                        self._reset()
                    else:
                        payload = json.dumps({
                            "__type": "com.amazonaws.dynamodb.v20120810#InternalServerError",
                            "message": "injected"}).encode()
                        self._write_response(500, "Internal Server Error",
                                             [("Content-Type", "application/x-amz-json-1.0")], payload)
                    return
                self._forward_and_relay(rec, body)

            def _forward_and_relay(self, rec, body, ordered=False):
                if ordered:
                    # Held PUTs go upstream one at a time, oldest first.
                    with proxy.mu:
                        idx = proxy.held_puts.index(rec)
                    while True:
                        with proxy.mu:
                            done = all(r.get("t_resp") is not None for r in proxy.held_puts[:idx])
                        if done or proxy.stop.is_set():
                            break
                        time.sleep(0.01)
                rec["t_fwd"] = proxy.now()
                try:
                    status, reason, headers, data = self._forward(body)
                except Exception as err:
                    rec["t_resp"] = proxy.now()
                    rec["status"] = 599
                    rec["upstream_error"] = repr(err)
                    self._reset()
                    return
                rec["t_resp"] = proxy.now()
                rec["status"] = status
                for k, v in headers:
                    if k.lower() == "etag":
                        rec["etag"] = v.strip('"')
                try:
                    self._write_response(status, reason, headers, data)
                except OSError:
                    rec["client_gone"] = True
                    self.close_connection = True

            def _dispatch(self):
                try:
                    self._handle()
                except (ConnectionError, OSError):
                    self.close_connection = True

            do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _dispatch

        return Handler

    def _fire(self, kind, rec):
        self.fire_info = {"kind": kind, "rec": rec}
        self.fired.set()
        if self.on_capture:
            try:
                self.on_capture(kind, rec)
            except Exception:
                pass


def _reset_socket(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# --------------------------------------------------------------------------- trial


class Worker:
    def __init__(self, worker_id, port, table, bucket, t0):
        self.worker_id = worker_id
        self.port = port
        self.dir = tempfile.mkdtemp(prefix="fw-%s-" % worker_id)
        self.journal = os.path.join(self.dir, "journal.jsonl")
        self.stderr_path = os.path.join(self.dir, "stderr.log")
        self.ready = threading.Event()
        self.t0 = t0
        endpoint = "http://127.0.0.1:%d" % port
        env = dict(os.environ)
        env.update({"PYTHONPATH": APP_DIR, "AWS_ENDPOINT_URL": endpoint, "AWS_DEFAULT_REGION": REGION,
                    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                    "PYTHONUNBUFFERED": "1"})
        cmd = [PYTHON, "-m", "fenced_worker", "--worker-id", worker_id, "--endpoint", endpoint,
               "--table", table, "--lock-key", LOCK_KEY, "--bucket", bucket,
               "--ledger-key", LEDGER_KEY, "--lease-ms", str(LEASE_MS),
               "--heartbeat-ms", str(HEARTBEAT_MS), "--refresh-ms", str(OBSERVER_REFRESH_MS),
               "--buffer-ms", str(TAKEOVER_BUFFER_MS), "--work-interval-ms", str(WORK_INTERVAL_MS),
               "--journal", self.journal]
        self.stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(cmd, cwd=self.dir, env=env, stdout=subprocess.PIPE,
                                     stderr=self.stderr, stdin=subprocess.DEVNULL)
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            if line.strip() == b"READY":
                self.ready.set()

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGCONT)
                self.proc.terminate()
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
            except ProcessLookupError:
                pass
        self.stderr.close()

    def journal_lines(self):
        out = []
        try:
            with open(self.journal, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            out.append({"_unparsable": line})
        except FileNotFoundError:
            pass
        return out

    def stderr_tail(self, n=25):
        try:
            with open(self.stderr_path, "rb") as f:
                lines = f.read().decode(errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""


def stop_all(workers):
    for w in workers:
        if w.proc.poll() is None:
            try:
                os.kill(w.proc.pid, signal.SIGCONT)
                w.proc.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 3.0
    for w in workers:
        try:
            w.proc.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            w.proc.kill()
            w.proc.wait(5)
        try:
            w.stderr.close()
        except Exception:
            pass


class Trial:
    def __init__(self, case, repeat):
        self.case = case
        self.repeat = repeat
        suffix = uuid.uuid4().hex[:8]
        self.table = "locks-%s" % suffix
        self.bucket = "fenced-ledger-%s" % suffix
        self.t0 = time.monotonic()
        self.ddb = aws("dynamodb")
        self.s3 = aws("s3", s3={"addressing_style": "path"})
        self.streams = aws("dynamodbstreams")
        self.failures = []
        self.notes = []
        self.proxies = {}
        self.workers = {}
        self.retired = []                 # (wid, Worker) stopped on purpose during the trial
        self.snapshot = None
        self.stream_records = None
        self.epoch_old_rvn = {}

    def now(self):
        return time.monotonic() - self.t0

    def fail(self, msg):
        self.failures.append(msg)

    # -- setup ---------------------------------------------------------------

    def setup(self):
        deploy(aws("cloudformation"), "seed-" + self.table,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               TableName=self.table, BucketName=self.bucket)
        for _ in range(100):
            st = self.ddb.describe_table(TableName=self.table)["Table"]["TableStatus"]
            if st == "ACTIVE":
                break
            time.sleep(0.1)
        seed = self.s3.put_object(Bucket=self.bucket, Key=LEDGER_KEY,
                                  Body=b'{"fence":0,"entries":[]}',
                                  ContentType="application/json")
        self.seed_etag = seed["ETag"].strip('"')
        self.seed_version = seed.get("VersionId")
        for i in (1, 2):
            wid = "worker-%d" % i
            self.proxies[wid] = Proxy(wid, PROXY_BASE_PORT + i, self.table, self.bucket, self.t0).start()

    def start_worker(self, wid):
        w = Worker(wid, PROXY_BASE_PORT + int(wid.split("-")[1]), self.table, self.bucket, self.t0)
        self.workers[wid] = w
        return w

    def sigterm(self, wid):
        """Stop a worker the way a deploy does; returns the trial time of the signal."""
        w = self.workers.pop(wid)
        self.retired.append((wid, w))
        t = self.now()
        try:
            os.kill(w.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return t

    def reap(self, wid, timeout_s=5.0):
        """Wait for a SIGTERM'd worker to exit; kill it if it does not."""
        for w_id, w in self.retired:
            if w_id != wid or w.proc.poll() is not None:
                continue
            try:
                w.proc.wait(timeout_s)
            except subprocess.TimeoutExpired:
                self.notes.append("%s was still running %.0f s after SIGTERM; killed" % (wid, timeout_s))
                w.proc.kill()
                w.proc.wait(5)

    # -- ground truth reads ---------------------------------------------------

    def lock_item(self):
        r = self.ddb.get_item(TableName=self.table, Key={"key": {"S": LOCK_KEY}}, ConsistentRead=True)
        item = r.get("Item")
        if not item:
            return None
        return {
            "owner": item.get("ownerName", {}).get("S"),
            "rvn": item.get("recordVersionNumber", {}).get("S"),
            "fence": int(item["fence"]["N"]) if "fence" in item else None,
        }

    def ledger_now(self):
        r = self.s3.get_object(Bucket=self.bucket, Key=LEDGER_KEY)
        return json.loads(r["Body"].read())

    def poll(self, pred, timeout_s, interval=0.1):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                v = pred()
            except Exception:
                v = None
            if v:
                return v
            time.sleep(interval)
        return None

    def warmup(self):
        w1 = self.start_worker("worker-1")
        if not w1.ready.wait(READY_TIMEOUT_MS / 1000.0):
            self.fail("worker-1 did not print READY within %ds" % (READY_TIMEOUT_MS // 1000))
            return False

        def held_and_warm():
            item = self.lock_item()
            if not item or not (item["owner"] or "").startswith("worker-1:"):
                return None
            entries = self.ledger_now().get("entries", [])
            if sum(1 for e in entries if e.get("writer") == "worker-1") >= WARMUP_ENTRIES:
                return item
            return None

        item = self.poll(held_and_warm, READY_TIMEOUT_MS / 1000.0)
        if not item:
            self.fail("worker-1 did not take the lock and append %d entries within %ds (stderr:\n%s)"
                      % (WARMUP_ENTRIES, READY_TIMEOUT_MS // 1000, w1.stderr_tail()))
            return False
        self.w1_owner = item["owner"]
        w2 = self.start_worker("worker-2")
        if not w2.ready.wait(READY_TIMEOUT_MS / 1000.0):
            self.fail("worker-2 did not print READY within %ds" % (READY_TIMEOUT_MS // 1000))
            return False
        time.sleep(0.5 + random.random())
        if not w1.alive():
            self.fail("worker-1 exited during warmup:\n%s" % w1.stderr_tail())
            return False
        return True

    # -- end of trial -----------------------------------------------------------

    def finish(self):
        for p in self.proxies.values():
            with p.mu:
                p.cut = True
        time.sleep(1.0)
        snap = {}
        try:
            snap["item"] = self.lock_item()
        except Exception as err:
            snap["item_error"] = repr(err)
        try:
            snap["versions"] = self.ledger_versions()
        except Exception as err:
            snap["versions_error"] = repr(err)
        self.snapshot = snap
        for wid, w in self.workers.items():
            if not w.alive():
                self.fail("%s exited before the end of the trial (code %s):\n%s"
                          % (wid, w.proc.returncode, w.stderr_tail()))
        stop_all([w for w in self.workers.values()] + [w for _, w in self.retired])
        for p in self.proxies.values():
            p.close()
        try:
            self.stream_records = self.read_stream()
        except Exception as err:
            self.stream_records = None
            self.notes.append("stream unavailable (%r); auditing lock transitions from proxy logs" % err)

    def ledger_versions(self):
        versions = []
        kw = {"Bucket": self.bucket, "Prefix": LEDGER_KEY}
        while True:
            r = self.s3.list_object_versions(**kw)
            for v in r.get("Versions", []):
                if v["Key"] == LEDGER_KEY:
                    versions.append(v)
            if not r.get("IsTruncated"):
                break
            kw["KeyMarker"] = r.get("NextKeyMarker")
            kw["VersionIdMarker"] = r.get("NextVersionIdMarker")
        out = []
        for v in versions:
            obj = self.s3.get_object(Bucket=self.bucket, Key=LEDGER_KEY, VersionId=v["VersionId"])
            raw = obj["Body"].read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            out.append({"version": v["VersionId"], "etag": v["ETag"].strip('"'),
                        "latest": v.get("IsLatest", False), "md5": hashlib.md5(raw).hexdigest(),
                        "body": body})
        return out

    def read_stream(self):
        arn = self.ddb.describe_table(TableName=self.table)["Table"].get("LatestStreamArn")
        if not arn:
            raise RuntimeError("no stream on table")
        desc = self.streams.describe_stream(StreamArn=arn)["StreamDescription"]
        records = []
        for shard in desc.get("Shards", []):
            it = self.streams.get_shard_iterator(StreamArn=arn, ShardId=shard["ShardId"],
                                                 ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            empty = 0
            while it and empty < 3:
                r = self.streams.get_records(ShardIterator=it, Limit=1000)
                recs = r.get("Records", [])
                records.extend(recs)
                empty = empty + 1 if not recs else 0
                it = r.get("NextShardIterator")
        records.sort(key=lambda r: int(r["dynamodb"].get("SequenceNumber", "0")))
        if not records:
            raise RuntimeError("stream returned no records")
        return records

    # -- audits ------------------------------------------------------------------

    def proxy_log(self, wid=None):
        out = []
        for k, p in self.proxies.items():
            if wid is None or k == wid:
                out.extend(p.records())
        out.sort(key=lambda r: r["t_recv"])
        return out

    def landed_lock_writes(self, wid):
        return [r for r in self.proxy_log(wid)
                if r["kind"] == "ddb" and r["lock_table"] and r["op"] in DDB_WRITE_OPS
                and r.get("t_fwd") is not None and r.get("status") == 200]

    def epochs(self):
        """[(owner, fence)] in order, plus violations, from the stream (or proxy fallback)."""
        violations = []
        epochs = []
        if self.stream_records is not None:
            written = {}
            for wid in self.proxies:
                s = set()
                for r in self.proxy_log(wid):
                    if r["kind"] == "ddb" and r["lock_table"] and r.get("t_fwd") is not None:
                        _strings(r.get("body", {}), s)
                written[wid] = s
            top = 0           # highest fence the item has carried
            owner_now = None  # current owner; None before the first write and after a delete
            for rec in self.stream_records:
                ev = rec.get("eventName")
                d = rec["dynamodb"]
                if d.get("Keys", {}).get("key", {}).get("S", LOCK_KEY) != LOCK_KEY:
                    continue
                if ev == "REMOVE":
                    owner_now = None
                    continue
                new = d.get("NewImage", {})
                old = d.get("OldImage")
                owner = new.get("ownerName", {}).get("S")
                fence = int(new["fence"]["N"]) if "fence" in new else None
                rvn = new.get("recordVersionNumber", {}).get("S")
                wid = (owner or "").split(":")[0]
                if wid not in written or rvn not in written[wid]:
                    violations.append("lock item write (owner %s, rvn %s) did not come through %s's "
                                      "endpoint" % (owner, rvn, wid or "any worker"))
                if fence is None:
                    violations.append("lock item written without a fence (owner %s)" % owner)
                    continue
                if not epochs and fence != 1:
                    violations.append("first acquisition wrote fence %s" % fence)
                if owner != owner_now:
                    if epochs and fence <= top:
                        violations.append("lock acquired by %s with fence %s, but fence %s had already "
                                          "been issued (fence must grow on every acquisition)"
                                          % (owner, fence, top))
                    epochs.append((owner, fence))
                    if old is not None:
                        self.epoch_old_rvn[(owner, fence)] = old.get("recordVersionNumber", {}).get("S")
                elif fence != epochs[-1][1]:
                    if fence < epochs[-1][1]:
                        violations.append("fence went back %s -> %s under the same owner (%s)"
                                          % (epochs[-1][1], fence, owner))
                    epochs.append((owner, fence))
                owner_now = owner
                top = max(top, fence)
        else:
            for r in sorted([r for wid in self.proxies for r in self.landed_lock_writes(wid)],
                            key=lambda r: r["t_fwd"]):
                if r["op"] != "PutItem":
                    continue
                item = r["body"].get("Item", {})
                owner = item.get("ownerName", {}).get("S")
                fence = int(item["fence"]["N"]) if "fence" in item else None
                if not epochs or epochs[-1] != (owner, fence):
                    epochs.append((owner, fence))
            item = (self.snapshot or {}).get("item")
            if item and (not epochs or epochs[-1] != (item["owner"], item["fence"])):
                epochs.append((item["owner"], item["fence"]))
            for a, b in zip(epochs, epochs[1:]):
                if a[1] is None or b[1] is None or b[1] < a[1] or (a[0] != b[0] and b[1] == a[1]):
                    violations.append("owner changed %s -> %s with fence %s -> %s" % (a[0], b[0], a[1], b[1]))
            # provenance of the final item
            if item:
                wid = (item["owner"] or "").split(":")[0]
                s = set()
                for r in self.proxy_log(wid):
                    if r["kind"] == "ddb" and r["lock_table"] and r.get("t_fwd") is not None:
                        _strings(r.get("body", {}), s)
                if item["rvn"] not in s:
                    violations.append("final lock item rvn did not come through %s's endpoint" % wid)
        return epochs, violations

    def audit_ledger(self, epochs):
        snap = self.snapshot or {}
        versions = snap.get("versions")
        if versions is None:
            self.fail("could not read ledger versions: %s" % snap.get("versions_error"))
            return None
        for v in versions:
            if v["body"] is None or not isinstance(v["body"].get("entries"), list):
                self.fail("ledger version %s is not a ledger document" % v["version"])
                return None
        latest = [v for v in versions if v["latest"]]
        final = latest[0] if latest else versions[0]
        fentries = final["body"]["entries"]
        # provenance of every non-seed version
        put_md5 = set()
        put_etag = set()
        for r in self.proxy_log():
            if r["kind"] == "s3" and r["ledger"] and r["op"] == "PUT" and r.get("status") == 200:
                put_md5.add(r.get("body_md5"))
                put_etag.add(r.get("etag"))
        for v in versions:
            if v["version"] == self.seed_version or (v["etag"] == self.seed_etag and not v["body"]["entries"]):
                continue
            if v["md5"] not in put_md5 and v["etag"] not in put_etag:
                self.fail("ledger version %s was not written through a worker's endpoint" % v["version"])
        # every version is a prefix of the final one
        key = lambda e: json.dumps(e, sort_keys=True)
        fkeys = [key(e) for e in fentries]
        for v in versions:
            ents = v["body"]["entries"]
            if [key(e) for e in ents] != fkeys[:len(ents)]:
                self.fail("ledger history was rewritten: version %s (%d entries, last %s) is not a "
                          "prefix of the final ledger (%d entries)"
                          % (v["version"], len(ents), ents[-1] if ents else None, len(fentries)))
        ordered = sorted(versions, key=lambda v: len(v["body"]["entries"]))
        tops = [v["body"].get("fence", 0) for v in ordered]
        for a, b in zip(tops, tops[1:]):
            if b < a:
                self.fail("ledger top-level fence went backwards (%s -> %s)" % (a, b))
                break
        prev = 0
        for i, e in enumerate(fentries):
            f = e.get("fence")
            if not isinstance(f, int):
                self.fail("ledger entry %d has no integer fence: %s" % (i, e))
                break
            if f < prev:
                self.fail("ledger entry %d %s (fence %d) follows a fence-%d entry" % (i, e, f, prev))
                break
            prev = f
        epoch_writer = {}
        for owner, fence in epochs:
            if owner and fence is not None:
                epoch_writer[fence] = owner.split(":")[0]
        for i, e in enumerate(fentries):
            w = epoch_writer.get(e.get("fence"))
            if w is None or w != e.get("writer"):
                self.fail("ledger entry %d %s does not match the lock epoch for fence %s (holder %s)"
                          % (i, e, e.get("fence"), w))
                break
        present = {(e.get("writer"), e.get("fence"), e.get("n")) for e in fentries}
        for wid, w in list(self.workers.items()) + self.retired:
            for line in w.journal_lines():
                k = (line.get("writer"), line.get("fence"), line.get("n"))
                if k not in present:
                    self.fail("%s journaled %s as acknowledged but it is not in the final ledger"
                              % (wid, {x: line.get(x) for x in ("writer", "fence", "n")}))
                    break
        return fentries

    def takeover_time(self):
        puts = [r for r in self.landed_lock_writes("worker-2") if r["op"] == "PutItem"]
        return puts[0]["t_fwd"] if puts else None

    def last_landed_heartbeat(self, before):
        ups = [r for r in self.landed_lock_writes("worker-1") if r["t_fwd"] < before]
        return ups[-1]["t_fwd"] if ups else None

    def check_takeover_timing(self, last_hb):
        t = self.takeover_time()
        if t is None:
            self.fail("worker-2 never took over the lock")
            return
        if last_hb is None:
            self.fail("no landed worker-1 heartbeat before the takeover")
            return
        gap = (t - last_hb) * 1000
        self.notes.append("takeover %.0f ms after worker-1's last applied heartbeat" % gap)
        if gap < LEASE_MS - EARLY_TAKEOVER_TOLERANCE_MS:
            self.fail("worker-2 took over %.0f ms after worker-1's last applied heartbeat "
                      "(less than one lease)" % gap)
        if gap > TAKEOVER_DEADLINE_MS:
            self.fail("worker-2 took over %.0f ms after worker-1's last applied heartbeat "
                      "(failover expectation is %d ms)" % (gap, TAKEOVER_DEADLINE_MS))

    def report(self):
        lines = ["case %s#%d" % (self.case, self.repeat)]
        lines += ["  note: " + n for n in self.notes]
        lines += ["  FAIL: " + f for f in self.failures]
        return "\n".join(lines)
