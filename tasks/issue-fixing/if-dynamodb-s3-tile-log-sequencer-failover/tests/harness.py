"""Drill harness for the tile-log worker verifier.

Everything here is independent of the code under /app: the tree, tile
and checkpoint formats are reimplemented, the service state is read
directly, and the workers are driven as black-box processes over their
published interfaces (HTTP, DynamoDB, S3, control channel).

Two pieces of trusted machinery sit between a worker and the emulator:

  * a control hub, owning the Unix socket a worker announces its pauses
    on (the worker blocks until the hub acknowledges), and
  * a record-write relay in front of DynamoDB, which forwards the
    worker's own request bytes untouched and can park one, release it,
    forward it while withholding the response, or never forward it.
"""

import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import boto3
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import http.client
from botocore.config import Config

# ---------------------------------------------------------------- codec

TILE_WIDTH = 4
SLACK_MS = 250
STARTUP_DEADLINE_S = 10.0
BARRIER_DEADLINE_S = 10.0
RECOVERY_DEADLINE_S = 10.0
PROGRESS_DEADLINE_S = 10.0
CONFLICT_DEADLINE_S = 5.0
REQUEST_DEADLINE_S = 60.0
QUIET_WINDOW_S = 1.0
POLL_INTERVAL_S = 0.05


def with_slack(deadline_s):
    return deadline_s + SLACK_MS / 1000.0


def leaf_hash(payload):
    return hashlib.sha256(b"\x00" + payload).digest()


def node_hash(left, right):
    return hashlib.sha256(b"\x01" + left + right).digest()


def empty_root():
    return hashlib.sha256(b"").digest()


def _split(n):
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def tree_root(payloads):
    payloads = list(payloads)
    if not payloads:
        return empty_root()

    def rec(lo, hi):
        if hi - lo == 1:
            return leaf_hash(payloads[lo])
        k = lo + _split(hi - lo)
        return node_hash(rec(lo, k), rec(k, hi))

    return rec(0, len(payloads))


def path_number(n):
    parts = ["%03d" % (n % 1000)]
    n //= 1000
    while n > 0:
        parts.append("x%03d" % (n % 1000))
        n //= 1000
    return "/".join(reversed(parts))


def data_tile_path(t, w):
    if w >= TILE_WIDTH:
        return "tile/data/" + path_number(t)
    return "tile/data/%s.p/%d" % (path_number(t), w)


def hash_tile_path(level, t, w):
    if w >= TILE_WIDTH:
        return "tile/%d/%s" % (level, path_number(t))
    return "tile/%d/%s.p/%d" % (level, path_number(t), w)


def entry_span(level):
    return TILE_WIDTH ** level


def tile_span(level):
    return TILE_WIDTH ** (level + 1)


def data_tiles_for_size(n):
    t, remaining = 0, n
    while remaining > 0:
        w = min(remaining, TILE_WIDTH)
        yield t, w
        remaining -= w
        t += 1


def hash_tiles_for_size(n):
    level = 0
    while True:
        s, ts = entry_span(level), tile_span(level)
        t, emitted = 0, False
        while t * ts + s <= n:
            w = min(TILE_WIDTH, (n - t * ts) // s)
            yield level, t, w
            emitted = True
            t += 1
        if not emitted:
            break
        level += 1


def entry_hash(payloads, lo, hi):
    if hi - lo == 1:
        return leaf_hash(payloads[lo])
    k = lo + _split(hi - lo)
    return node_hash(entry_hash(payloads, lo, k), entry_hash(payloads, k, hi))


def format_checkpoint(origin, size, root, time_):
    return ("%s\n%d\n%s\n%d\n" % (origin, size, root.hex(), time_)).encode()


def parse_checkpoint(blob):
    lines = bytes(blob).decode("ascii").splitlines()
    if len(lines) != 4:
        raise ValueError("checkpoint must have 4 lines")
    return {"origin": lines[0], "size": int(lines[1]),
            "root": bytes.fromhex(lines[2]), "time": int(lines[3])}


def decode_data_tile(blob):
    return [base64.b64decode(x) for x in json.loads(bytes(blob).decode())]


def decode_hash_tile(blob):
    blob = bytes(blob)
    if len(blob) % 32:
        raise ValueError("bad hash tile length %d" % len(blob))
    return [blob[i:i + 32] for i in range(0, len(blob), 32)]


# ------------------------------------------------------------- services


class Services:
    """Direct, independent access to the emulator."""

    def __init__(self, endpoint="http://localhost:4566", region="us-east-1"):
        self.endpoint = endpoint
        self.ddb = boto3.client(
            "dynamodb", endpoint_url=endpoint, region_name=region,
            config=Config(retries={"max_attempts": 3}))
        self.s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name=region,
            config=Config(retries={"max_attempts": 3},
                          s3={"addressing_style": "path"}))

    def create_table(self, name):
        deploy(boto3.client("cloudformation", endpoint_url=self.endpoint,
                            region_name=self.ddb.meta.region_name),
               "seed-" + name,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               TableName=name)
        for _ in range(100):
            try:
                desc = self.ddb.describe_table(TableName=name)
                if desc["Table"]["TableStatus"] == "ACTIVE":
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("table %s never became ACTIVE" % name)

    def create_bucket(self, name):
        try:
            self.s3.head_bucket(Bucket=name)
        except Exception:
            deploy(boto3.client("cloudformation", endpoint_url=self.endpoint,
                                region_name=self.s3.meta.region_name),
                   "seed-" + name,
                   Path(__file__).parent / "cdk_seed/bucket.template.json",
                   BucketName=name)

    def put_record(self, table, log_id, checkpoint):
        self.ddb.put_item(TableName=table, Item={
            "logID": {"B": log_id}, "checkpoint": {"B": checkpoint}})

    def get_record(self, table, log_id):
        resp = self.ddb.get_item(TableName=table,
                                 Key={"logID": {"B": log_id}},
                                 ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return None
        return item["checkpoint"]["B"]

    def put_object(self, bucket, key, blob):
        self.s3.put_object(Bucket=bucket, Key=key, Body=blob)

    def get_object(self, bucket, key):
        try:
            resp = self.s3.get_object(Bucket=bucket, Key=key)
            return resp["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None
        except Exception:
            return None

    def healthy(self):
        try:
            urllib.request.urlopen(self.endpoint + "/_floci/health",
                                   timeout=5).read()
            return True
        except Exception:
            return False


class Namespace:
    """Fresh table + bucket + audit prefix for one drill."""

    def __init__(self, services, name, origin):
        self.services = services
        self.name = name
        self.origin = origin
        self.table = "drill-%s-records" % name
        self.bucket = "drill-%s-objects" % name
        self.audit_bucket = "drill-%s-audit" % name
        self.prefix = "log"
        self.log_id = hashlib.sha256(name.encode()).digest()
        services.create_table(self.table)
        services.create_bucket(self.bucket)
        services.create_bucket(self.audit_bucket)
        self.c0 = format_checkpoint(origin, 0, empty_root(), 0)
        services.put_record(self.table, self.log_id, self.c0)
        services.put_object(self.bucket, self.key("checkpoint"), self.c0)

    def key(self, name):
        return self.prefix + "/" + name

    def record(self):
        return self.services.get_record(self.table, self.log_id)

    def object(self, name):
        return self.services.get_object(self.bucket, self.key(name))

    def audit(self, milestone, payload):
        blob = json.dumps(payload, indent=1, sort_keys=True,
                          default=lambda o: o.hex() if isinstance(o, bytes)
                          else str(o)).encode()
        self.services.put_object(
            self.audit_bucket, "%s/%s.json" % (self.name, milestone), blob)

    # -- independent verification of the published tree -----------------

    def read_published_payloads(self, size):
        payloads = []
        tiles = {}
        for t, w in data_tiles_for_size(size):
            blob = self.object(data_tile_path(t, w))
            if blob is None:
                raise KeyError("missing data tile %s" % data_tile_path(t, w))
            tiles[data_tile_path(t, w)] = blob
            payloads.extend(decode_data_tile(blob))
        if len(payloads) != size:
            raise ValueError("data tiles carry %d entries, expected %d"
                             % (len(payloads), size))
        return payloads, tiles

    def verify_public_tree(self, expected_checkpoint, expected_payloads):
        """Full independent check of the published state.

        Returns a list of human-readable failure strings; empty is a
        pass.  Checks the published checkpoint bytes, the record, the
        data tiles against the expected leaves, and every hash tile
        against a root recomputed from the published data.
        """
        problems = []
        expected = parse_checkpoint(expected_checkpoint)
        size = expected["size"]

        record = self.record()
        if record != expected_checkpoint:
            problems.append(
                "record is %s, expected %s"
                % (record.hex() if record else None,
                   expected_checkpoint.hex()))
        published = self.object("checkpoint")
        if published != expected_checkpoint:
            problems.append(
                "published checkpoint is %s, expected %s"
                % (published.hex() if published else None,
                   expected_checkpoint.hex()))
        if problems:
            return problems

        try:
            payloads, _ = self.read_published_payloads(size)
        except Exception as exc:
            problems.append("data tiles unreadable: %r" % (exc,))
            return problems

        if expected_payloads is not None and payloads != list(
                expected_payloads):
            problems.append("published leaves are not the expected leaves "
                            "in order")
        if tree_root(payloads) != expected["root"]:
            problems.append("published data tiles recompute to %s, "
                            "checkpoint root is %s"
                            % (tree_root(payloads).hex(),
                               expected["root"].hex()))
        for level, t, w in hash_tiles_for_size(size):
            blob = self.object(hash_tile_path(level, t, w))
            if blob is None:
                problems.append("missing hash tile %s"
                                % hash_tile_path(level, t, w))
                continue
            try:
                hashes = decode_hash_tile(blob)
            except ValueError as exc:
                problems.append("%s: %r" % (hash_tile_path(level, t, w),
                                            exc))
                continue
            if len(hashes) != w:
                problems.append("hash tile %s carries %d hashes, expected %d"
                                % (hash_tile_path(level, t, w), len(hashes),
                                   w))
                continue
            base = t * tile_span(level)
            for j, got in enumerate(hashes):
                want = entry_hash(payloads, base + j * entry_span(level),
                                  base + (j + 1) * entry_span(level))
                if got != want:
                    problems.append(
                        "hash tile %s entry %d is %s, tree says %s"
                        % (hash_tile_path(level, t, w), j, got.hex(),
                           want.hex()))
        return problems


# ---------------------------------------------------------- record relay


class RecordRelay(threading.Thread):
    """Transparent relay in front of DynamoDB for one worker.

    Forwards the worker's own request bytes untouched.  For the first
    write to the checkpoint table it can be told to:

      park           hold the request before it is forwarded
      park_withhold  hold it, then forward it on release and hold the
                     response until a second release
      withhold       forward it at once, then hold the response
      suppress       hold it and never forward it

    Anything else, and any later write, is relayed immediately.
    """

    def __init__(self, table, host="127.0.0.1"):
        super().__init__(daemon=True)
        self.table = table
        self.mode = "passthrough"
        self.arrived = threading.Event()
        self.responded = threading.Event()
        self.release = threading.Event()
        self.deliver = threading.Event()
        self.observed = []          # dicts describing gated requests
        self.responses = []         # dicts describing gated responses
        self.upstream_host = "localhost"
        self.upstream_port = 4566
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        self.endpoint = "http://%s:%d" % self.sock.getsockname()
        self.stop_flag = False

    def arm(self, mode):
        self.mode = mode

    def run(self):
        while not self.stop_flag:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve, args=(conn,),
                             daemon=True).start()

    def shutdown(self):
        self.stop_flag = True
        self.release.set()
        self.deliver.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # -- HTTP plumbing ---------------------------------------------------

    def read_request(self, conn):
        # Long enough that a worker held at a pause for a whole drill
        # keeps its pooled connection.
        conn.settimeout(REQUEST_DEADLINE_S + 90)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin1")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.decode("latin1").strip().lower()] = \
                    v.decode("latin1").strip()
        if "content-length" not in headers:
            return None  # not a shape we gate; caller closes
        length = int(headers["content-length"])
        body = rest
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            body += chunk
        return request_line, headers, body[:length], body[length:]

    def upstream(self, request_line, headers, body):
        conn = http.client.HTTPConnection(self.upstream_host,
                                          self.upstream_port, timeout=30)
        method, path, _ = request_line.split(" ", 2)
        skip = {"host", "content-length", "connection",
                "transfer-encoding", "accept-encoding"}
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            if k.lower() in skip:
                continue
            conn.putheader(k, v)
        conn.putheader("Host", "%s:%d" % (self.upstream_host,
                                          self.upstream_port))
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        status, reason = resp.status, resp.reason
        resp_headers = [(k, v) for k, v in resp.getheaders()
                        if k.lower() not in ("transfer-encoding",
                                             "connection",
                                             "content-length")]
        resp_body = resp.read()
        conn.close()
        return status, reason, resp_headers, resp_body

    def send_response(self, conn, status, reason, headers, body):
        head = "HTTP/1.1 %d %s\r\n" % (status, reason)
        headers = list(headers) + [("Content-Length", str(len(body)))]
        for k, v in headers:
            head += "%s: %s\r\n" % (k, v)
        try:
            conn.sendall(head.encode("latin1") + b"\r\n" + body)
            return True
        except OSError:
            return False

    # -- gating ----------------------------------------------------------

    def is_fence_write(self, request_line, headers, body):
        if not request_line.startswith("POST "):
            return False
        target = headers.get("x-amz-target", "")
        if not (target.endswith(".PutItem") or target.endswith(".UpdateItem")
                or target.endswith(".TransactWriteItems")):
            return False
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return False
        return payload.get("TableName") == self.table

    def describe(self, request_line, headers, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}
        checkpoint = payload.get("Item", {}).get("checkpoint", {}).get("B")
        return {
            "request": request_line,
            "target": headers.get("x-amz-target", ""),
            "table": payload.get("TableName"),
            "conditional": "ConditionExpression" in payload,
            "condition": payload.get("ConditionExpression"),
            "item_checkpoint_bytes":
                len(checkpoint) * 3 // 4 if isinstance(checkpoint, str)
                else None,
        }

    def serve(self, conn):
        gated = False
        try:
            while True:
                parsed = self.read_request(conn)
                if parsed is None:
                    break
                request_line, headers, body, extra = parsed
                if extra:
                    break  # pipelining not expected; be safe
                fence = (not gated) and self.is_fence_write(
                    request_line, headers, body)
                if fence and self.mode in ("park", "park_withhold",
                                           "withhold", "suppress"):
                    gated = True
                    info = self.describe(request_line, headers, body)
                    info["mode"] = self.mode
                    self.observed.append(info)
                    self.arrived.set()
                    if self.mode == "suppress":
                        self.release.wait(timeout=REQUEST_DEADLINE_S)
                        break
                    if self.mode in ("park", "park_withhold"):
                        self.release.wait(timeout=REQUEST_DEADLINE_S)
                        if self.stop_flag:
                            break
                    status, reason, rheaders, rbody = self.upstream(
                        request_line, headers, body)
                    rinfo = {"status": status,
                             "conditional_failed":
                                 b"ConditionalCheckFailedException" in rbody}
                    self.responses.append(rinfo)
                    self.responded.set()
                    if self.mode in ("withhold", "park_withhold"):
                        self.deliver.wait(timeout=REQUEST_DEADLINE_S)
                        if self.stop_flag:
                            break
                    self.send_response(conn, status, reason, rheaders,
                                       rbody)
                    continue
                status, reason, rheaders, rbody = self.upstream(
                    request_line, headers, body)
                if not self.send_response(conn, status, reason, rheaders,
                                          rbody):
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def wait_arrived(self, timeout):
        return self.arrived.wait(timeout=timeout)

    def wait_responded(self, timeout):
        return self.responded.wait(timeout=timeout)

    def forward(self):
        self.release.set()

    def deliver_response(self):
        self.deliver.set()


# ---------------------------------------------------------- control hub


class ControlHub(threading.Thread):
    """Owns one worker's control socket."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        self.auto = {"batch_assembled"}
        self.holds = set()
        self.events = []
        self.returned = set()
        self.cond = threading.Condition()
        self.release_events = {}
        try:
            os.unlink(path)
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve, args=(conn,),
                             daemon=True).start()

    def shutdown(self):
        self.stop_flag = True
        for ev in list(self.release_events.values()):
            ev.set()
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def serve(self, conn):
        conn.settimeout(REQUEST_DEADLINE_S + 30)
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            line, _, _ = buf.partition(b"\n")
            event = json.loads(line.decode("utf-8"))
        except Exception:
            return
        release = threading.Event()
        with self.cond:
            event["_release"] = release
            self.events.append(event)
            self.release_events[id(event)] = release
            self.cond.notify_all()
        if event["pause"] not in self.holds and \
                event["pause"] in self.auto:
            release.set()
        try:
            release.wait(timeout=REQUEST_DEADLINE_S + 30)
        except Exception:
            pass
        try:
            if not self.stop_flag:
                conn.sendall(b"ack\n")
        except OSError:
            pass
        with self.cond:
            self.release_events.pop(id(event), None)
        try:
            conn.close()
        except OSError:
            pass

    def next(self, name, timeout):
        """Wait for the next event with this pause name.

        Each event is returned once: a worker that announces the same
        pause twice (an attempt that ends in conflict, then a later
        attempt that commits) yields its announcements in order.
        """
        deadline = time.monotonic() + timeout
        with self.cond:
            seen = 0
            while True:
                for event in self.events[seen:]:
                    if event["pause"] == name and \
                            id(event) not in self.returned:
                        self.returned.add(id(event))
                        return event
                seen = len(self.events)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)

    def wait_any(self, timeout):
        deadline = time.monotonic() + timeout
        with self.cond:
            while not self.events:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(remaining)
            return self.events[0]

    def release(self, event):
        event["_release"].set()

    def release_all(self, name):
        with self.cond:
            for event in self.events:
                if event["pause"] == name:
                    event["_release"].set()


# ------------------------------------------------------------- workers


class Worker:
    """One worker process, its control hub and its record relay."""

    def __init__(self, namespace, worker_id, port, relay_mode=None):
        self.ns = namespace
        self.worker_id = worker_id
        self.dir = tempfile.mkdtemp(prefix="worker-%s-" % worker_id)
        self.log_path = os.path.join(self.dir, "worker.log")
        self.log_file = open(self.log_path, "wb")
        self.relay = RecordRelay(namespace.table)
        self.relay.start()
        if relay_mode:
            self.relay.arm(relay_mode)
        self.control_path = os.path.join(self.dir, "control.sock")
        self.hub = ControlHub(self.control_path)
        self.hub.start()
        self.port = port
        self.config = {
            "worker_id": worker_id,
            "origin": namespace.origin,
            "table": namespace.table,
            "log_id": namespace.log_id.hex(),
            "bucket": namespace.bucket,
            "prefix": namespace.prefix,
            "ddb_endpoint": self.relay.endpoint,
            "s3_endpoint": "http://localhost:4566",
            "listen_host": "127.0.0.1",
            "listen_port": port,
            "control_socket": self.control_path,
            "checkpoint_time_step": 1,
            "tile_width": TILE_WIDTH,
        }
        self.config_path = os.path.join(self.dir, "config.json")
        with open(self.config_path, "w") as fh:
            json.dump(self.config, fh, indent=1)
        self.proc = None

    def start(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = "/app"
        self.proc = subprocess.Popen(
            ["python3", "/app/sequencer.py", "--config", self.config_path],
            cwd=self.dir, env=env,
            stdout=self.log_file, stderr=subprocess.STDOUT)
        return self.proc

    def wait_listening(self, timeout):
        """Wait until the worker's HTTP endpoint accepts connections."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port),
                                               timeout=1):
                    return True
            except OSError:
                time.sleep(POLL_INTERVAL_S)
        return False

    def submit_async(self, request_id, entries):
        """POST /submit in a thread; returns a result box."""
        box = {}

        def run():
            body = json.dumps({"request_id": request_id,
                               "entries": [base64.b64encode(e).decode()
                                           for e in entries]}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/submit" % self.port, data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req,
                                            timeout=120) as r:
                    box["response"] = json.loads(r.read().decode())
            except Exception as exc:  # killed worker, reset connection
                box["error"] = repr(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        box["thread"] = thread
        return box

    def wait_response(self, box, timeout):
        deadline = time.monotonic() + timeout
        thread = box["thread"]
        while time.monotonic() < deadline:
            if not thread.is_alive() and ("response" in box or
                                          "error" in box):
                return box.get("response"), box.get("error")
            time.sleep(POLL_INTERVAL_S)
        return None, "timeout after %.1fs" % timeout

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGKILL)
            except OSError:
                pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def tail(self, limit=4000):
        try:
            with open(self.log_path, "rb") as fh:
                return fh.read()[-limit:].decode("utf-8", "replace")
        except OSError:
            return ""

    def shutdown(self):
        self.kill()
        self.hub.shutdown()
        self.relay.shutdown()
        try:
            self.log_file.close()
        except OSError:
            pass


def free_ports(count):
    socks = []
    ports = []
    for _ in range(count):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


# ------------------------------------------------------------ recording


class Checks:
    """Collects named outcomes; nothing here is fail-fast."""

    def __init__(self, group):
        self.group = group
        self.records = []

    def record(self, name, ok, detail=""):
        self.records.append((name, bool(ok), str(detail)))
        mark = "ok  " if ok else "FAIL"
        print("[%s] %s | %s %s" % (self.group, mark, name, detail),
              flush=True)
        return bool(ok)

    def expect(self, name, condition, detail=""):
        return self.record(name, condition, detail)

    def failures(self):
        return [(n, d) for n, ok, d in self.records if not ok]

    def summary(self):
        total = len(self.records)
        bad = len(self.failures())
        print("[%s] %d checks, %d failed" % (self.group, total, bad),
              flush=True)
        return bad
