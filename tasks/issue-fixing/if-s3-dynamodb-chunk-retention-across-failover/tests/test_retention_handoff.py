"""Verification harness for the retention-compactor failover fix.

Drives one retention run under the placement schedule the incident
describes: the prepared batch is held before publication, the first
placement is killed shortly after publication commits, the replacement
is killed right after its first successful object delete, the next
placement is killed hard while it is still waiting out the deletion
grace on the last remaining chunk, and a final placement finishes the
run. The store carries a second index table of the same tenant, whose
snapshots turn over on their own schedule mid-run. Three independent
query frontends serve snapshots throughout (one per table, plus one
following the new snapshot). Every assertion reads real S3/DynamoDB
state or the frontends' durable audit rows; nothing is taken from
solver-owned code or reports.
"""
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from decimal import Decimal
from decimal import Decimal as D

import pytest
from pathlib import Path
from cdk_seed.cfn_deploy import deploy

EP = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = "us-east-1"

HOLD = 12.0            # prepared batch held before publication goes live
FIRST_KILL = 2.0       # first placement killed this long after publication
REPLACEMENT = 1.0      # successor started this long after a confirmed death
DELAY = 8.0            # the run's deletion grace (delete_delay_s)
COMPLETION = 36.0      # reclamation budget after publication
SLACK = 0.25           # observation slack for polling deadlines only
READER_REFRESH = 6.0   # frontends may serve the old snapshot this long
NEIGHBOR_FLIP = 10.0   # the tenant's other table turns over this late
NEIGHBOR_ADVANCE = 13.0  # its next snapshot lists one target again
NEIGHBOR_FINAL = 24.0    # and this turnover retires that snapshot
LAST_KILL = 5.0          # the waiting placement is killed this late
                         # after the final turnover, mid-grace
POLL = 0.1
B_WINDOW = NEIGHBOR_FINAL + DELAY + 4.0  # wait for 2nd placement's 1st delete

N_TARGETS, N_LIVE, N_STAGED = 2, 2, 1

REWARD = "/logs/verifier/reward.txt"
VDIR = "/logs/verifier"


def now():
    return time.time()


def f(v):
    return float(v) if isinstance(v, Decimal) else v


def sleep_until(t):
    delay = t - now()
    if delay > 0:
        time.sleep(delay)


# --------------------------------------------------------------------------
# harness plumbing
# --------------------------------------------------------------------------
class SeamHub(threading.Thread):
    """Accepts the service's pause announcements and stages them."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.events = []
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.release_prepared = threading.Event()
        self.ack_everything = threading.Event()
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,),
                             daemon=True).start()

    def handle(self, conn):
        try:
            conn.settimeout(60)
            stream = conn.makefile("rb")
            line = stream.readline()
            if not line:
                return
            msg = json.loads(line)
        except Exception:
            return
        name = msg.pop("seam", "?")
        ev = {"ts": now(), "seam": name}
        ev.update(msg)
        with self.lock:
            self.events.append(ev)
        self.q.put(ev)
        epoch = 0
        try:
            epoch = int(ev.get("epoch", 0))
        except (TypeError, ValueError):
            epoch = 0
        if self.ack_everything.is_set() or name in ("ready",):
            self._reply(conn)
        elif name == "prepared":
            self.release_prepared.wait(timeout=180)
            self._reply(conn)
        elif name == "published":
            if epoch == 1:
                self._park(conn)      # the driver kills this placement here
            else:
                self._reply(conn)
        elif name == "object_deleted":
            if epoch == 2:
                self._park(conn)      # and here, after the first delete
            else:
                self._reply(conn)
        else:
            self._reply(conn)

    def _reply(self, conn):
        try:
            conn.sendall(b"go\n")
            conn.close()
        except OSError:
            pass

    def _park(self, conn):
        deadline = now() + 240
        while now() < deadline and not self.ack_everything.is_set():
            time.sleep(0.1)
        self._reply(conn)

    def close(self):
        self.stopped.set()
        self.ack_everything.set()
        try:
            self.sock.close()
        except OSError:
            pass


class ReaderProc:
    def __init__(self, path):
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(1)
        self.listener.settimeout(30)
        self.q = queue.Queue()
        self.conn = None

    def accept(self):
        self.conn, _ = self.listener.accept()

        def pump():
            stream = self.conn.makefile("rb")
            for line in stream:
                line = line.strip()
                if line:
                    try:
                        self.q.put(json.loads(line))
                    except ValueError:
                        pass

        threading.Thread(target=pump, daemon=True).start()

    def send(self, obj):
        self.conn.sendall((json.dumps(obj) + "\n").encode())

    def wait_ev(self, kind, timeout):
        deadline = now() + timeout
        seen = []
        while now() < deadline:
            try:
                msg = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            seen.append(msg)
            if msg.get("ev") == kind:
                return msg
        return None


def wait_seam(hub, pred, timeout):
    """Wait for a pause announcement matching pred, scanning the log."""
    deadline = now() + timeout
    while now() < deadline:
        for ev in list(hub.events):
            if pred(ev):
                return ev
        try:
            hub.q.get(timeout=0.1)
        except queue.Empty:
            pass
    return None


# --------------------------------------------------------------------------
# fixture construction
# --------------------------------------------------------------------------
class Ctx:
    pass


def build_fixture(base):
    import boto3
    from botocore.config import Config

    cfg = Config(retries={"max_attempts": 4, "mode": "standard"})
    s3 = boto3.client("s3", endpoint_url=EP, region_name=REGION, config=cfg)
    ddb = boto3.resource("dynamodb", endpoint_url=EP, region_name=REGION,
                         config=cfg)
    u = uuid.uuid4().hex
    names = dict(bucket="cs-%s" % u[:12], table="runs-%s" % u[:8],
                 run="r-%s" % u[:8], batch="b-%s" % u[:8],
                 tenant="t-%s" % u[:6], table_id="idx-%s" % u[:6],
                 v0="g%s" % u[:8], v1="h%s" % u[8:16],
                 table_id2="idx-%s" % u[14:20], nv0="k%s" % u[16:24],
                 nv1="m%s" % u[24:32], nv2="p%s" % u[4:12],
                 nv3="q%s" % u[20:28])

    deploy(boto3.client("cloudformation", endpoint_url=EP, region_name=REGION),
           "seed-" + u, Path(__file__).parent / "cdk_seed/seed.template.json",
           BucketName=names["bucket"], TableName=names["table"])
    table = ddb.Table(names["table"])
    for _ in range(60):
        try:
            table.load()
            break
        except Exception:
            time.sleep(0.5)

    import secrets as _secrets

    def make_chunk():
        key = "chunks/%s" % _secrets.token_hex(8)
        payload = _secrets.token_bytes(200)
        return key, payload, hashlib.sha256(payload).hexdigest()

    targets, live, staged = [], [], []
    digests = {}
    for _ in range(N_TARGETS):
        k, p, d = make_chunk()
        s3.put_object(Bucket=names["bucket"], Key=k, Body=p)
        targets.append({"key": k, "digest": d, "bytes": len(p)})
        digests[k] = d
    for _ in range(N_LIVE):
        k, p, d = make_chunk()
        s3.put_object(Bucket=names["bucket"], Key=k, Body=p)
        live.append({"key": k, "digest": d, "bytes": len(p),
                     "expires_at": time.time() + 3600})
        digests[k] = d
    for _ in range(N_STAGED):
        k, p, d = make_chunk()
        s3.put_object(Bucket=names["bucket"], Key=k, Body=p)
        staged.append(k)
        digests[k] = d
    for entry in targets:
        entry["expires_at"] = time.time() - 500

    index_key = "index/%s/%s/%s.json" % (names["tenant"], names["table_id"],
                                         names["v0"])
    index0 = {"version": names["v0"], "chunks": targets + live}
    index0_bytes = json.dumps(index0).encode()
    s3.put_object(Bucket=names["bucket"], Key=index_key, Body=index0_bytes)

    # the tenant's other index table carries the same boundary-spanning
    # chunks in its own snapshot, under its own authoritative pointer
    n_key = "index/%s/%s/%s.json" % (names["tenant"], names["table_id2"],
                                     names["nv0"])
    n0 = {"version": names["nv0"], "chunks": targets}
    n0_bytes = json.dumps(n0).encode()
    s3.put_object(Bucket=names["bucket"], Key=n_key, Body=n0_bytes)

    pk = "RUN#%s" % names["run"]
    table.put_item(Item={"pk": pk, "sk": "HEAD", "version": names["v0"],
                         "index_key": index_key})
    table.put_item(Item={"pk": pk, "sk": "HEAD#%s" % names["table_id2"],
                         "version": names["nv0"], "index_key": n_key})
    table.put_item(Item={"pk": pk, "sk": "OWNER", "epoch": 1, "worker": ""})
    table.put_item(Item={
        "pk": pk, "sk": "META", "run": names["run"],
        "tenant": names["tenant"], "table_id": names["table_id"],
        "batch": names["batch"], "cutoff": D(str(time.time())),
        "delete_delay_s": D(str(DELAY)), "poll_s": D(str(POLL)),
        "chunk_prefix": "chunks", "index_prefix": "index",
        "marker_prefix": "markers", "out_version": names["v1"]})

    ctx = Ctx()
    side = "index/%s/%s/%%s.json" % (names["tenant"], names["table_id2"])
    ctx.__dict__.update(
        s3=s3, ddb=ddb, table=table, pk=pk, names=names,
        targets=[e["key"] for e in targets], live=[e["key"] for e in live],
        staged=staged, digests=digests,
        index0_key=index_key, index0_bytes=index0_bytes,
        expected_v1_key="index/%s/%s/%s.json" % (names["tenant"],
                                                 names["table_id"],
                                                 names["v1"]),
        relist=targets[1],
        neighbor0_key=n_key, neighbor0_bytes=n0_bytes,
        neighbor1_key=side % names["nv1"],
        neighbor2_key=side % names["nv2"],
        neighbor3_key=side % names["nv3"],
        marker_key="markers/%s.json" % names["batch"])
    return ctx


# --------------------------------------------------------------------------
# observation helpers
# --------------------------------------------------------------------------
def s3_get(ctx, key):
    try:
        resp = ctx.s3.get_object(Bucket=ctx.names["bucket"], Key=key)
        return resp["Body"].read()
    except Exception:
        return None


def s3_exists(ctx, key):
    try:
        ctx.s3.head_object(Bucket=ctx.names["bucket"], Key=key)
        return True
    except Exception:
        return False


def marker_keyset(blob):
    """The set of chunk keys a deletion marker enumerates, tolerating its
    JSON shape: any array of strings comparing equal to a target set."""
    if not blob:
        return None
    try:
        doc = json.loads(blob)
    except ValueError:
        return None

    def arrays(node):
        if isinstance(node, list):
            yield node
            for item in node:
                yield from arrays(item)
        elif isinstance(node, dict):
            for item in node.values():
                yield from arrays(item)

    for arr in arrays(doc):
        if arr and all(isinstance(x, str) for x in arr):
            return set(arr)
    return None


def batch_item(ctx):
    return ctx.table.get_item(
        Key={"pk": ctx.pk, "sk": "BATCH#%s" % ctx.names["batch"]},
        ConsistentRead=True).get("Item")


def head_item(ctx):
    return ctx.table.get_item(Key={"pk": ctx.pk, "sk": "HEAD"},
                              ConsistentRead=True).get("Item")


def audit_rows(ctx):
    rows = []
    resp = ctx.table.query(
        KeyConditionExpression="pk = :pk AND begins_with(sk, :a)",
        ExpressionAttributeValues={":pk": ctx.pk, ":a": "AUDIT#"})
    rows += resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = ctx.table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :a)",
            ExpressionAttributeValues={":pk": ctx.pk, ":a": "AUDIT#"},
            ExclusiveStartKey=resp["LastEvaluatedKey"])
        rows += resp.get("Items", [])
    return rows


def start_worker(ctx, tag, epoch, hub_path, logs):
    workdir = os.path.join(logs, "work-%s" % tag)
    os.makedirs(workdir, exist_ok=True)
    out = open(os.path.join(logs, "%s.log" % tag), "wb")
    proc = subprocess.Popen(
        [sys.executable, "/app/worker.py", "--endpoint", EP,
         "--region", REGION, "--bucket", ctx.names["bucket"],
         "--table", ctx.names["table"], "--run-id", ctx.names["run"],
         "--worker-id", tag, "--epoch", str(epoch),
         "--work-dir", workdir],
        cwd=workdir, stdout=out, stderr=subprocess.STDOUT,
        env=dict(os.environ, SEAM_SOCKET=hub_path, PYTHONPATH="/app",
                 PYTHONUNBUFFERED="1"))
    return proc, workdir


def kill_proc(proc):
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def bump_epoch(ctx, old, new, worker):
    for _ in range(10):
        try:
            ctx.table.update_item(
                Key={"pk": ctx.pk, "sk": "OWNER"},
                ConditionExpression="epoch = :old",
                UpdateExpression="SET epoch = :new, worker = :w",
                ExpressionAttributeValues={":old": old, ":new": new,
                                           ":w": worker})
            return True
        except Exception:
            time.sleep(0.2)
    return False


def monitor_loop(ctx, stop, rec):
    first_absence = {}
    control_missing = {}
    status_log = []
    last_status = None
    last_ctrl = 0.0
    while not stop.is_set():
        t = now()
        for key in ctx.targets:
            if key not in first_absence and not s3_exists(ctx, key):
                first_absence[key] = t
        if t - last_ctrl >= 0.5:
            for key in ctx.live + ctx.staged:
                if key not in control_missing and not s3_exists(ctx, key):
                    control_missing[key] = t
            last_ctrl = t
        item = batch_item(ctx)
        status = (item or {}).get("status")
        if status != last_status:
            status_log.append({"ts": t, "status": status})
            last_status = status
        time.sleep(POLL)
    rec["first_absence"] = first_absence
    rec["control_missing"] = control_missing
    rec["status_log"] = status_log


# --------------------------------------------------------------------------
# the schedule
# --------------------------------------------------------------------------
def neighbor_publish(ctx, rec, P):
    """The tenant's other index table turns its snapshots over on its own
    schedule, independently of the run being graded. Nothing in the graded
    run drives this; the run only has to live with it. Every turnover is
    observed with a consistent read, and the per-key floor below moves to
    the latest turnover that stopped serving the key."""
    n_sk = "HEAD#%s" % ctx.names["table_id2"]
    plan = (
        (NEIGHBOR_FLIP, ctx.neighbor1_key,
         {"version": ctx.names["nv1"], "chunks": []}),
        (NEIGHBOR_ADVANCE, ctx.neighbor2_key,
         {"version": ctx.names["nv2"], "chunks": [ctx.relist]}),
        (NEIGHBOR_FINAL, ctx.neighbor3_key,
         {"version": ctx.names["nv3"], "chunks": []}),
    )
    floors = {key: P for key in ctx.targets}
    served = set(ctx.targets)
    try:
        prev_version = ctx.names["nv0"]
        for at, key, doc in plan:
            sleep_until(P + at)
            body = json.dumps(doc).encode()
            ctx.s3.put_object(Bucket=ctx.names["bucket"], Key=key, Body=body)
            rec.setdefault("side_writes", {})[key] = body
            for _ in range(10):
                try:
                    ctx.table.update_item(
                        Key={"pk": ctx.pk, "sk": n_sk},
                        ConditionExpression="#v = :was",
                        UpdateExpression="SET #v = :nowv, #k = :key",
                        ExpressionAttributeNames={"#v": "version",
                                                  "#k": "index_key"},
                        ExpressionAttributeValues={
                            ":was": prev_version, ":nowv": doc["version"],
                            ":key": key},
                    )
                    break
                except Exception:
                    time.sleep(0.2)
            observed = None
            deadline = now() + 20
            while now() < deadline:
                item = ctx.table.get_item(Key={"pk": ctx.pk, "sk": n_sk},
                                          ConsistentRead=True).get("Item")
                if item and item.get("version") == doc["version"]:
                    observed = now()
                    break
                time.sleep(0.03)
            if observed is None:
                rec["error"] = "the other table stopped mid-turnover"
                return
            now_serves = {e["key"] for e in doc["chunks"]}
            for key in served - now_serves:
                floors[key] = max(floors[key], observed)
            served = now_serves
            prev_version = doc["version"]
            rec.setdefault("turnovers", []).append(
                {"attempt": P + at, "observed": observed,
                 "version": doc["version"]})
            if rec.get("P2") is None:
                rec["P2"] = observed
        rec["P3"] = observed
        rec["floors"] = floors
        item = ctx.table.get_item(Key={"pk": ctx.pk, "sk": n_sk},
                                  ConsistentRead=True).get("Item")
        rec["neighbor"] = {"flip_attempt": P + NEIGHBOR_FLIP,
                           "observed": rec["P2"], "head": dict(item or {})}
    except Exception as exc:
        rec["error"] = "neighbor turnover failed: %r" % (exc,)


def run_scenario(base):
    rec = {"P": None, "P2": None, "resume": False, "error": None,
           "env_fail": False}

    # preflight: the emulator must be live before anything is graded
    try:
        import boto3
        probe = boto3.client("s3", endpoint_url=EP, region_name=REGION)
        ok = False
        for _ in range(5):
            try:
                probe.list_buckets()
                ok = True
                break
            except Exception:
                time.sleep(2)
        if not ok:
            raise RuntimeError("s3 endpoint not answering")
    except Exception as exc:
        rec["env_fail"] = True
        rec["error"] = "ENVIRONMENT: floci not reachable: %r" % (exc,)
        try:
            os.makedirs(VDIR, exist_ok=True)
            with open(REWARD, "w") as fh:
                fh.write("0\n")
            with open(os.path.join(VDIR, "environment_failure.txt"), "w") as fh:
                fh.write(rec["error"] + "\n")
        except OSError:
            pass
        return rec

    ctx = build_fixture(base)
    rec.update(names=dict(ctx.names), targets=list(ctx.targets),
               live=list(ctx.live), staged=list(ctx.staged),
               digests=dict(ctx.digests), index0_key=ctx.index0_key,
               index0_bytes=ctx.index0_bytes,
               expected_v1_key=ctx.expected_v1_key,
               neighbor0_key=ctx.neighbor0_key,
               neighbor1_key=ctx.neighbor1_key,
               neighbor2_key=ctx.neighbor2_key,
               neighbor3_key=ctx.neighbor3_key,
               neighbor0_bytes=ctx.neighbor0_bytes,
               marker_key=ctx.marker_key)

    logs = os.path.join(base, "logs")
    os.makedirs(logs, exist_ok=True)
    hub = SeamHub(os.path.join(logs, "seam.sock"))
    hub.start()

    readers = {}
    reader_specs = (("r-old", "stale", "HEAD"),
                    ("r-new", "follow", "HEAD"),
                    ("r-side", "stale", "HEAD#%s" % ctx.names["table_id2"]))
    for name, mode, head_sk in reader_specs:
        rp = ReaderProc(os.path.join(logs, "%s.sock" % name))
        out = open(os.path.join(logs, "%s.log" % name), "wb")
        proc = subprocess.Popen(
            [sys.executable, "/tests/reader.py", "--socket", rp.path,
             "--endpoint", EP, "--region", REGION,
             "--bucket", ctx.names["bucket"], "--table", ctx.names["table"],
             "--run", ctx.names["run"], "--reader", name, "--mode", mode,
             "--head-sk", head_sk,
             "--refresh-after-flip", str(READER_REFRESH),
             "--flight", "1.0", "--poll", str(POLL)],
            cwd=logs, stdout=out, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED="1"))
        rp.accept()
        readers[name] = (rp, proc)
        rec.setdefault("reader_pinned", {})[name] = rp.wait_ev("pinned", 30)

    stop = threading.Event()
    mon = threading.Thread(target=monitor_loop, args=(ctx, stop, rec),
                           daemon=True)
    mon.start()

    procs = [readers[n][1] for n in readers]
    dirs = []
    try:
        # -- placement 1: prepare, hold, publish, die --------------------
        a, dira = start_worker(ctx, "w-1", 1, hub.path, logs)
        procs.append(a)
        dirs.append(dira)
        ready = wait_seam(hub, lambda e: e["seam"] == "ready"
                          and e.get("worker") == "w-1", 30)
        rec["ready_a"] = bool(ready)
        prepared = wait_seam(hub, lambda e: e["seam"] == "prepared"
                             and e.get("worker") == "w-1", 45)
        rec["prepared"] = prepared
        if prepared:
            rec["prepared_keys"] = sorted(prepared.get("keys", []) or [])
            rec["prepared_marker"] = s3_get(ctx, ctx.marker_key)
        sleep_until((prepared or {"ts": now()})["ts"] + HOLD)
        rec["released_at"] = now()
        hub.release_prepared.set()

        deadline = now() + 45
        while now() < deadline:
            if (head_item(ctx) or {}).get("version") == ctx.names["v1"]:
                rec["P"] = now()
                break
            time.sleep(0.03)

        if rec["P"] is None:
            rec["error"] = "publication never committed"
            return rec

        turnover = threading.Thread(target=neighbor_publish,
                                    args=(ctx, rec, rec["P"]), daemon=True)
        turnover.start()

        sleep_until(rec["P"] + FIRST_KILL)
        kill_proc(a)
        shutil.rmtree(dira, ignore_errors=True)
        bump_epoch(ctx, 1, 2, "w-2")
        rec["handoff1"] = {
            "ts": now(),
            "targets": {k: s3_exists(ctx, k) for k in ctx.targets},
            "marker": s3_get(ctx, ctx.marker_key),
            "batch": batch_item(ctx),
            "head": head_item(ctx),
            "neighbor_head": ctx.table.get_item(
                Key={"pk": ctx.pk, "sk": "HEAD#%s" % ctx.names["table_id2"]},
                ConsistentRead=True).get("Item"),
        }
        v1blob = s3_get(ctx, (rec["handoff1"]["head"] or {}).get("index_key"))
        try:
            rec["handoff1"]["v1_keys"] = sorted(
                e["key"] for e in json.loads(v1blob)["chunks"])
        except Exception:
            rec["handoff1"]["v1_keys"] = None

        # -- placement 2: recover, sweep once, die -----------------------
        sleep_until(rec["P"] + FIRST_KILL + REPLACEMENT + 0.6)
        b, dirb = start_worker(ctx, "w-2", 2, hub.path, logs)
        procs.append(b)
        dirs.append(dirb)
        rec["ready_b"] = bool(wait_seam(
            hub, lambda e: e["seam"] == "ready" and e.get("worker") == "w-2",
            30))
        deleted = wait_seam(
            hub, lambda e: e["seam"] == "object_deleted"
            and e.get("worker") == "w-2", B_WINDOW)
        rec["second_delete"] = deleted
        if deleted:
            key = deleted.get("key")
            for _ in range(4):
                if not s3_exists(ctx, key):
                    break
                time.sleep(0.2)
            rec["resume"] = True
            rec["kill2"] = {
                "ts": now(), "key": key,
                "key_absent": not s3_exists(ctx, key),
                "targets": {k: s3_exists(ctx, k) for k in ctx.targets},
                "batch": batch_item(ctx),
                "marker": s3_get(ctx, ctx.marker_key),
            }
        kill_proc(b)
        shutil.rmtree(dirb, ignore_errors=True)
        bump_epoch(ctx, 2, 3, "w-3")
        hub.ack_everything.set()

        # -- placement 3: recover, wait on the last chunk, die ----------
        kill2_t = now()
        sleep_until(kill2_t + REPLACEMENT)
        c, dirc = start_worker(ctx, "w-3", 3, hub.path, logs)
        procs.append(c)
        dirs.append(dirc)
        rec["ready_c"] = bool(wait_seam(
            hub, lambda e: e["seam"] == "ready" and e.get("worker") == "w-3",
            30))

        deadline = now() + 40
        while now() < deadline and rec.get("P3") is None:
            time.sleep(0.03)
        kill3_at = (rec.get("P3") or now()) + LAST_KILL
        sleep_until(kill3_at)
        kill_proc(c)
        shutil.rmtree(dirc, ignore_errors=True)
        bump_epoch(ctx, 3, 4, "w-4")
        rec["handoff2"] = {
            "ts": now(),
            "targets": {k: s3_exists(ctx, k) for k in ctx.targets},
            "batch": batch_item(ctx),
            "marker": s3_get(ctx, ctx.marker_key),
        }

        # -- placement 4: finish ------------------------------------------
        sleep_until(kill3_at + REPLACEMENT)
        d, dird = start_worker(ctx, "w-4", 4, hub.path, logs)
        procs.append(d)
        dirs.append(dird)
        rec["ready_d"] = bool(wait_seam(
            hub, lambda e: e["seam"] == "ready" and e.get("worker") == "w-4",
            30))

        sleep_until(rec["P"] + COMPLETION + SLACK)
        rec["final"] = {
            "ts": now(),
            "targets": {k: s3_exists(ctx, k) for k in ctx.targets},
            "chunk_list": sorted(
                k for k in
                [o["Key"] for page in
                 ctx.s3.get_paginator("list_objects_v2").paginate(
                     Bucket=ctx.names["bucket"], Prefix="chunks/")
                 for o in page.get("Contents", [])]),
            "digests": {k: hashlib.sha256(s3_get(ctx, k) or b"").hexdigest()
                        for k in ctx.live + ctx.staged},
            "batch": batch_item(ctx),
            "head": head_item(ctx),
            "owner": ctx.table.get_item(
                Key={"pk": ctx.pk, "sk": "OWNER"},
                ConsistentRead=True).get("Item"),
            "marker": s3_get(ctx, ctx.marker_key),
            "marker_list": sorted(
                o["Key"] for page in
                ctx.s3.get_paginator("list_objects_v2").paginate(
                    Bucket=ctx.names["bucket"], Prefix="markers/")
                for o in page.get("Contents", [])),
            "index_list": sorted(
                o["Key"] for page in
                ctx.s3.get_paginator("list_objects_v2").paginate(
                    Bucket=ctx.names["bucket"], Prefix="index/")
                for o in page.get("Contents", [])),
            "v0_bytes": s3_get(ctx, ctx.index0_key),
            "n0_bytes": s3_get(ctx, ctx.neighbor0_key),
            "side_bytes": {key: s3_get(ctx, key) for key in
                           (ctx.neighbor1_key, ctx.neighbor2_key,
                            ctx.neighbor3_key)},
            "neighbor_head": ctx.table.get_item(
                Key={"pk": ctx.pk, "sk": "HEAD#%s" % ctx.names["table_id2"]},
                ConsistentRead=True).get("Item"),
        }
        v1blob = s3_get(ctx, (rec["final"]["head"] or {}).get("index_key"))
        try:
            rec["final"]["v1_keys"] = sorted(
                e["key"] for e in json.loads(v1blob)["chunks"])
        except Exception:
            rec["final"]["v1_keys"] = None
    finally:
        stop.set()
        mon.join(timeout=5)
        for name, (rp, proc) in list(readers.items()):
            try:
                rp.send({"op": "stop"})
            except OSError:
                pass
            done = rp.wait_ev("done", 15)
            rec.setdefault("reader_done", {})[name] = done
            kill_proc(proc)
        for proc in procs:
            kill_proc(proc)
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
        hub.close()
        with hub.lock:
            rec["events"] = [dict(ev) for ev in hub.events]
        try:
            rec["audit"] = [
                {k: (float(v) if isinstance(v, Decimal) else v)
                 for k, v in row.items()} for row in audit_rows(ctx)]
        except Exception as exc:
            rec["audit_error"] = repr(exc)
        try:
            os.makedirs(VDIR, exist_ok=True)
            with open(os.path.join(VDIR, "timeline.json"), "w") as fh:
                json.dump({k: v for k, v in rec.items()
                           if k not in ("prepared_marker",)}, fh,
                          default=str, indent=1)
            with open(os.path.join(base, "timeline.json"), "w") as fh:
                json.dump(rec, fh, default=str, indent=1)
        except OSError:
            pass
    return rec


_RESULT = {}


@pytest.fixture(scope="session")
def R(tmp_path_factory):
    if "rec" not in _RESULT:
        base = str(tmp_path_factory.mktemp("scenario"))
        _RESULT["rec"] = run_scenario(base)
    return _RESULT["rec"]


def require_schedule(R, need="P"):
    if R.get("env_fail"):
        pytest.fail("environment failure: %s" % R.get("error"))
    if R.get(need) is None and need != "resume":
        pytest.fail("schedule did not run: %s" % (R.get("error")
                                                  or need))


# --------------------------------------------------------------------------
# graded groups
# --------------------------------------------------------------------------
def test_pause_points_observed(R):
    require_schedule(R)
    assert R.get("ready_a"), "first placement never announced startup"
    prepared = R.get("prepared")
    assert prepared, "first placement never announced a prepared batch"
    assert R.get("prepared_keys") == sorted(R["targets"]), \
        "the prepared batch did not enumerate exactly the expired chunks"
    published = [e for e in seam_events(R) if e["seam"] == "published"]
    assert published, "first placement never announced publication"
    assert all(e["ts"] >= R["released_at"] for e in published), \
        "publication was announced before the held batch was released"


def seam_events(R):
    return R.get("events", [])


def test_prefetch_window_clean(R):
    require_schedule(R)
    P = R["P"]
    for key, ts in (R.get("first_absence") or {}).items():
        assert ts >= P, "chunk %s disappeared before publication" % key
    hearts = [r for r in R.get("audit", []) if r.get("kind") == "heart"]
    hold_hearts = [h for h in hearts if f(h.get("t1", 0)) <= P]
    assert hold_hearts, "no frontend observations during the prepare hold"
    for h in hold_hearts:
        assert f(h.get("problems", 0)) == 0, \
            "frontend saw problems while the old snapshot was authoritative"


def test_publication_state_at_first_handoff(R):
    require_schedule(R)
    h1 = R.get("handoff1") or {}
    assert (h1.get("batch") or {}).get("status") == "PUBLISHED"
    assert h1.get("head", {}).get("version") == R["names"]["v1"]
    assert sorted(h1.get("v1_keys") or []) == sorted(R["live"]), \
        "published snapshot membership is wrong"
    for key, present in (h1.get("targets") or {}).items():
        assert present, "target %s already gone at the first handoff" % key
    for blob in (R.get("prepared_marker"), h1.get("marker")):
        assert blob, "no deletion marker in the bucket while the batch ran"
        assert marker_keyset(blob) == set(R["targets"]), \
            "deletion marker does not enumerate exactly the authorized set"


def test_deletion_timing(R):
    require_schedule(R)
    require_schedule(R, "P2")
    require_schedule(R, "P3")
    P = R["P"]
    floors = R.get("floors") or {}
    fa = R.get("first_absence") or {}
    assert len(fa) == len(R["targets"]), \
        "not every authorized chunk was deleted"
    for key, ts in fa.items():
        assert ts >= P + DELAY - SLACK, \
            "chunk %s was deleted before the grace was honored" % key
        assert ts >= floors.get(key, P) + DELAY - SLACK, \
            "chunk %s was deleted before the grace was honored after " \
            "the last index retirement that served it" % key
    problems = [r for r in R.get("audit", [])
                if r.get("kind") == "query"]
    assert not problems, "frontend query failures: %s" % (
        [dict(r) for r in problems],)


def test_progress_at_second_kill(R):
    require_schedule(R)
    assert R.get("resume"), \
        "no replacement resumed the pending deletion work within its window"
    k2 = R.get("kill2") or {}
    assert k2.get("key") in R["targets"]
    assert k2.get("key_absent"), \
        "announced delete did not take effect at the service"
    gone = [k for k, present in (k2.get("targets") or {}).items() if not present]
    assert gone, "no partial progress survived the second placement"
    assert (k2.get("batch") or {}).get("status") != "DONE", \
        "batch claimed completion before the work was finished"
    assert marker_keyset(k2.get("marker") or b"") == set(R["targets"])


def test_completion(R):
    require_schedule(R)
    P = R["P"]
    fin = R.get("final") or {}
    assert all(not present for present in
               (fin.get("targets") or {}).values()), \
        "authorized chunks still present after the completion budget"
    assert sorted(fin.get("chunk_list") or []) == \
        sorted(R["live"] + R["staged"]), \
        "chunk listing is not exactly the retained plus staged objects"
    status_log = R.get("status_log") or []
    done_ts = [e["ts"] for e in status_log if e.get("status") == "DONE"]
    assert (fin.get("batch") or {}).get("status") == "DONE", \
        "batch record is not DONE after the completion budget"
    assert done_ts and done_ts[0] <= P + COMPLETION + SLACK, \
        "completion was recorded after the budget"
    assert marker_keyset(fin.get("marker") or b"") == set(R["targets"]), \
        "completed batch's deletion marker is not readable and exact"
    assert fin.get("marker_list") == [R["marker_key"]]


def test_frontend_records(R):
    require_schedule(R)
    require_schedule(R, "P2")
    P = R["P"]
    P2 = R["P2"]
    summaries = {r["reader"]: r for r in R.get("audit", [])
                 if r.get("kind") == "summary"}
    hearts = [r for r in R.get("audit", []) if r.get("kind") == "heart"]
    for name, s in summaries.items():
        assert f(s.get("missing", 0)) == 0 and f(s.get("corrupt", 0)) == 0, \
            "frontend %s failed object reads: %s" % (name, dict(s))
    old = summaries.get("r-old") or {}
    new = summaries.get("r-new") or {}
    side = summaries.get("r-side") or {}
    assert old, "stale frontend never reported"
    assert f(old.get("last_old_ts", 0)) >= P + READER_REFRESH - 1.0, \
        "stale frontend stopped serving the old snapshot too early"
    assert f(old.get("last_old_ts", 1e18)) <= P + READER_REFRESH + 1.4, \
        "stale frontend served the old snapshot past its bound"
    assert new and f(new.get("first_new_ts", 1e18)) <= P + 1.5, \
        "following frontend never picked up the published snapshot"
    assert side, "the other table's frontend never reported"
    assert f(side.get("last_old_ts", 0)) >= P2 + READER_REFRESH - 1.0, \
        "the other table's frontend stopped serving its snapshot too early"
    assert f(side.get("last_old_ts", 1e18)) <= P2 + READER_REFRESH + 1.4, \
        "the other table's frontend served its snapshot past its bound"
    assert hearts, "no frontend heartbeat evidence"
    per = {}
    for h in hearts:
        per.setdefault(h.get("reader"), []).append(h)
    for name, rows in per.items():
        rows.sort(key=lambda h: f(h.get("t1", 0)))
        for a, b in zip(rows, rows[1:]):
            assert f(b.get("t1", 0)) - f(a.get("t1", 0)) <= 2.5, \
                "frontend %s has an observation gap" % name
        assert f(rows[-1].get("t1", 0)) >= P + COMPLETION - 5, \
            "frontend %s stopped observing early" % name


def test_preservation(R):
    require_schedule(R)
    fin = R.get("final") or {}
    assert not (R.get("control_missing") or {}), \
        "retained or staged objects disappeared: %s" % (
            R.get("control_missing"),)
    for key in R["live"] + R["staged"]:
        assert fin.get("digests", {}).get(key) == R["digests"][key], \
            "retained object %s no longer has its original bytes" % key
    assert fin.get("v0_bytes") == R["index0_bytes"], \
        "an existing index snapshot was modified"
    assert fin.get("n0_bytes") == R["neighbor0_bytes"], \
        "the other table's snapshot was modified"
    for key, blob in (R.get("side_writes") or {}).items():
        assert (fin.get("side_bytes") or {}).get(key) == blob, \
            "the other table's snapshot %s was modified" % key
    assert sorted(fin.get("index_list") or []) == \
        sorted([R["index0_key"], R["expected_v1_key"],
                R["neighbor0_key"], R["neighbor1_key"],
                R["neighbor2_key"], R["neighbor3_key"]]), \
        "index snapshots are not exactly the original and published ones"
    assert (fin.get("owner") or {}).get("epoch") == 4
