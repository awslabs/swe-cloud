"""Process driver, direct bucket reads and the verifier's own chain-walk restore."""
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.config import Config

FLOCI_PORT = 4566
APP = os.environ.get("STRATA_APP", "/app")
NODE = os.path.join(APP, "node")

ROW_PAYLOAD_BYTES = 1024
SMALL_OBJECT_MAX_BYTES = 4096
COMMIT_ACK_DEADLINE_S = 2.0
CHECKPOINT_RETURN_DEADLINE_S = 2.0
HOLD_ENGAGE_DEADLINE_S = 5.0
DEP_VISIBLE_WAIT_S = 8.0
SNAPSHOT_COMPLETE_DEADLINE_S = 10.0
BOOT_READY_DEADLINE_S = 15.0
SHUTDOWN_DEADLINE_S = 15.0
RESTORE_DEADLINE_S = 30.0
MAX_CHAIN_DEPTH = 100
RESTORE_REFUSE_EXIT_CODE = 3

GEN_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
BATCH_RE = re.compile(r"^(\d{12})-(\d{12})-(\d+)\.jsonl$")
WRITE_METHODS = ("PUT", "POST", "DELETE")

ROOT = tempfile.mkdtemp(prefix="strata-verify-")


def allowlist(name):
    return re.compile(r"^" + re.escape(name) + r"-(" + GEN_RE + r")/(\.dep|\.changecounter|db\.sqlite|"
                      r"\d{12}-\d{12}-\d+\.jsonl)$")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def private_dir(tag):
    d = os.path.join(ROOT, f"{tag}-{uuid.uuid4().hex[:10]}")
    os.makedirs(d)
    return d


def hide(path):
    """Moves a dead node's directory to a random private path."""
    dest = os.path.join(ROOT, "hidden-" + uuid.uuid4().hex)
    shutil.move(path, dest)
    return dest


def direct_client():
    return boto3.client(
        "s3", endpoint_url=f"http://localhost:{FLOCI_PORT}", region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
        config=Config(s3={"addressing_style": "path"},
                      retries={"max_attempts": 4, "mode": "standard"}))


def payload(i):
    base = f"row-{i:06d}-"
    return (base * (ROW_PAYLOAD_BYTES // len(base) + 1))[:ROW_PAYLOAD_BYTES]


def proc_env(endpoint, home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update({
        "S3_ENDPOINT": endpoint, "AWS_ENDPOINT_URL": endpoint,
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1", "PYTHONPATH": APP,
        "HOME": home, "TMPDIR": home,
    })
    return env


def http_json(method, url, body=None, timeout=10.0):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:  # timeout, refused
        return None, {"error": repr(e)}, time.monotonic() - t0
    elapsed = time.monotonic() - t0
    try:
        obj = json.loads(raw or b"{}")
    except ValueError:
        obj = {"raw": raw[:200].decode(errors="replace")}
    return status, obj, elapsed


class Node:
    def __init__(self, db_dir, bucket, name, endpoint, logdir):
        self.db_dir, self.bucket, self.name, self.endpoint = db_dir, bucket, name, endpoint
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.logdir = logdir
        self.proc = None
        self.log_path = None

    def start(self):
        home = private_dir("home")
        self.log_path = os.path.join(self.logdir, f"serve-{uuid.uuid4().hex[:6]}.log")
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, NODE, "serve", "--db-dir", self.db_dir, "--bucket", self.bucket,
             "--db", self.name, "--port", str(self.port)],
            cwd=home, env=proc_env(self.endpoint, home), stdout=self._log,
            stderr=subprocess.STDOUT)
        return self

    def wait_healthy(self, deadline_s=BOOT_READY_DEADLINE_S):
        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline_s:
            if self.proc.poll() is not None:
                raise AssertionError(f"node serve exited with {self.proc.returncode} during boot; "
                                     f"log tail: {self.tail()}")
            status, obj, _ = http_json("GET", self.url + "/health", timeout=1.0)
            if status == 200:
                return obj
            time.sleep(0.1)
        raise AssertionError(f"node serve not healthy within {deadline_s}s; log tail: {self.tail()}")

    def commit(self, i, timeout=4.0):
        return http_json("POST", self.url + "/commit", {"id": i, "payload": payload(i)},
                         timeout=timeout)

    def checkpoint(self):
        return http_json("POST", self.url + "/checkpoint", {}, timeout=4.0)

    def sigterm(self, deadline_s=SHUTDOWN_DEADLINE_S):
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(deadline_s)
        except subprocess.TimeoutExpired:
            self.kill()
            return None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)

    def tail(self, n=1500):
        try:
            with open(self.log_path, "rb") as f:
                return f.read()[-n:].decode(errors="replace")
        except OSError:
            return ""


def run_restore(bucket, name, endpoint, logdir):
    """Runs `node restore` into a new empty dir; returns a dict with rc, out (parsed), timing."""
    home = private_dir("rhome")
    target = os.path.join(private_dir("restore"), "db")
    os.makedirs(target)
    t0 = time.time()
    try:
        cp = subprocess.run(
            [sys.executable, NODE, "restore", "--db-dir", target, "--bucket", bucket, "--db", name],
            cwd=home, env=proc_env(endpoint, home), capture_output=True, timeout=RESTORE_DEADLINE_S)
        rc, stdout, stderr = cp.returncode, cp.stdout.decode(errors="replace"), \
            cp.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired as e:
        rc, stdout, stderr = None, (e.stdout or b"").decode(errors="replace"), "timeout"
    t1 = time.time()
    out = None
    for line in reversed(stdout.strip().splitlines()):
        try:
            cand = json.loads(line)
        except ValueError:
            continue
        if isinstance(cand, dict):
            out = cand
            break
    with open(os.path.join(logdir, f"restore-{uuid.uuid4().hex[:6]}.log"), "w") as f:
        f.write(f"rc={rc}\nstdout:\n{stdout}\nstderr:\n{stderr}\n")
    return {"rc": rc, "out": out, "t0": t0, "t1": t1, "dir": target,
            "stderr_tail": stderr[-800:]}


# -------------------------------------------------------------------- bucket reads

def listing(s3, bucket):
    """Full-bucket listing as {key: (etag, size)}, in key order."""
    out = {}
    kwargs = {"Bucket": bucket}
    while True:
        resp = s3.list_objects_v2(**kwargs)
        for o in resp.get("Contents", []):
            out[o["Key"]] = (o.get("ETag"), o.get("Size"))
        if not resp.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]
    return dict(sorted(out.items()))


def generations(keys, name):
    """Generation ids of `name` in listing order (newest first), with their object names."""
    pat = re.compile(r"^" + re.escape(name) + r"-(" + GEN_RE + r")/(.+)$")
    order, objs = [], {}
    for k in sorted(keys):
        m = pat.match(k)
        if not m:
            continue
        g = m.group(1)
        if g not in objs:
            order.append(g)
            objs[g] = []
        objs[g].append(m.group(2))
    return order, objs


def get_bytes(s3, bucket, key):
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def read_snapshot(s3, bucket, key):
    data = get_bytes(s3, bucket, key)
    fd, path = tempfile.mkstemp(dir=ROOT, suffix=".sqlite")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        conn = sqlite3.connect(path)
        try:
            meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
            rows = set(r[0] for r in conn.execute("SELECT id FROM rows"))
        finally:
            conn.close()
    finally:
        os.remove(path)
    return int(meta["seq"]), int(meta.get("head", 0)), rows


def frame_batches(objs):
    out = []
    for o in objs:
        m = BATCH_RE.match(o)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), o))
    return sorted(out)


def read_records(s3, bucket, key):
    return [json.loads(line) for line in get_bytes(s3, bucket, key).decode().splitlines()
            if line.strip()]


def reference_restore(s3, bucket, name):
    """The bucket's state by the layout contract: head = first-listed generation; follow .dep
    to the nearest generation holding db.sqlite + .changecounter (or a root with no .dep);
    replay each generation's frames from frame 1 contiguously, oldest first."""
    order, objs = generations(listing(s3, bucket).keys(), name)
    if not order:
        return {"generation": None, "seq": 0, "head": 0, "ids": []}
    head = order[0]
    stack, base, cur = [], None, head
    while cur is not None:
        stack.append(cur)
        names = objs.get(cur, [])
        if "db.sqlite" in names and ".changecounter" in names:
            base = read_snapshot(s3, bucket, f"{name}-{cur}/db.sqlite")
            break
        if len(stack) > MAX_CHAIN_DEPTH:
            return {"refuse": True}
        if ".dep" in names:
            cur = get_bytes(s3, bucket, f"{name}-{cur}/.dep").decode("ascii").strip()
        else:
            cur = None
    seq, last_id, ids = base if base else (0, 0, set())
    ids = set(ids)
    for g in reversed(stack):
        expected = 1
        for first, last, o in frame_batches(objs.get(g, [])):
            if first != expected:
                break
            for rec in read_records(s3, bucket, f"{name}-{g}/{o}"):
                ids.add(int(rec["id"]))
                seq, last_id = int(rec["seq"]), int(rec["id"])
            expected = last + 1
    return {"generation": head, "seq": seq, "head": last_id, "ids": sorted(ids)}


def wait_for(pred, timeout, interval=0.1):
    t0 = time.monotonic()
    while True:
        v = pred()
        if v:
            return v, time.monotonic() - t0
        if time.monotonic() - t0 >= timeout:
            return None, time.monotonic() - t0
        time.sleep(interval)


def history_problems(s3, bucket, name):
    """Replays the whole chain, from the newest generation back along .dep to the database's
    first generation, ignoring snapshots: frames must run contiguously from 1 in every
    generation, and seq must count commits (each record's seq is the previous one's + 1). Every
    snapshot on the chain must be a state that history passed through."""
    order, objs = generations(listing(s3, bucket).keys(), name)
    if not order:
        return []
    chain, cur = [], order[0]
    while cur is not None:
        if cur in chain or len(chain) > MAX_CHAIN_DEPTH:
            return [f"the .dep chain from {order[0]} does not end"]
        if cur not in objs:
            return [f"the .dep chain from {order[0]} names {cur}, which is not in the bucket"]
        chain.append(cur)
        cur = (get_bytes(s3, bucket, f"{name}-{cur}/.dep").decode("ascii", "replace").strip()
               if ".dep" in objs[cur] else None)
    problems = []
    seq, last_id, ids = 0, 0, set()
    states = {0: (0, frozenset())}
    for g in reversed(chain):
        expected = 1
        for first, last, o in frame_batches(objs[g]):
            if first != expected:
                problems.append(f"{g}: frame batch {o} after frame {expected - 1}")
                break
            recs = read_records(s3, bucket, f"{name}-{g}/{o}")
            if [int(r["frame"]) for r in recs] != list(range(first, last + 1)):
                problems.append(f"{g}: {o} does not hold frames {first}..{last}")
                break
            for r in recs:
                if int(r["seq"]) != seq + 1:
                    problems.append(f"{g}: frame {r['frame']} carries seq {r['seq']} after seq "
                                    f"{seq}")
                seq, last_id = int(r["seq"]), int(r["id"])
                ids.add(last_id)
                states[seq] = (last_id, frozenset(ids))
            expected = last + 1
        if problems:
            break
    for g in chain:
        if "db.sqlite" in objs[g] and ".changecounter" in objs[g]:
            s_seq, s_head, s_rows = read_snapshot(s3, bucket, f"{name}-{g}/db.sqlite")
            if s_seq not in states or states[s_seq] != (s_head, frozenset(s_rows)):
                problems.append(f"snapshot of {g} (seq {s_seq}) is not a state of the chain's "
                                f"history")
    return problems


# -------------------------------------------------------------------- deployed history

_INVERT_SECONDS = 253370761200
_INVERT_NANOS = 999999999


def generation_at(unix_seconds):
    """A generation id for a generation created at `unix_seconds` (UUIDv7 of the inverted
    timestamp, as the README describes): an earlier time lists later."""
    seconds, nanos = int(unix_seconds), int((unix_seconds % 1) * 1e9)
    millis = (_INVERT_SECONDS - seconds) * 1000 + (_INVERT_NANOS - nanos) // 1_000_000
    b = bytearray(16)
    b[0:6] = millis.to_bytes(6, "big")
    rnd = os.urandom(10)
    b[6] = 0x70 | (rnd[0] & 0x0F)
    b[7] = rnd[1]
    b[8] = 0x80 | (rnd[2] & 0x3F)
    b[9:16] = rnd[3:10]
    return str(uuid.UUID(bytes=bytes(b)))


def snapshot_bytes(records):
    """A db.sqlite snapshot holding the state after `records` (each {"seq","id","payload"})."""
    fd, path = tempfile.mkstemp(dir=ROOT, suffix=".sqlite")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE rows(id INTEGER PRIMARY KEY, payload TEXT);"
                           "CREATE TABLE meta(k TEXT PRIMARY KEY, v INTEGER);")
        seq = head = 0
        for r in records:
            conn.execute("INSERT OR REPLACE INTO rows(id, payload) VALUES (?, ?)",
                         (r["id"], r["payload"]))
            seq, head = r["seq"], r["id"]
        conn.executemany("INSERT INTO meta(k, v) VALUES (?, ?)", [("seq", seq), ("head", head)])
        conn.commit()
    finally:
        conn.close()
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return data, seq


def seed_generation(s3, bucket, name, gen, created, dep=None, base=None, records=()):
    """Writes one generation the way the deployed writer left it: an optional .dep, an optional
    complete snapshot of the state `base` (list of records), and one frame batch per record
    (frames from 1). `created` is the unix time of the generation."""
    pre = f"{name}-{gen}/"
    if dep is not None:
        s3.put_object(Bucket=bucket, Key=pre + ".dep", Body=dep.encode("ascii"))
    if base is not None:
        data, seq = snapshot_bytes(base)
        s3.put_object(Bucket=bucket, Key=pre + "db.sqlite", Body=data)
        s3.put_object(Bucket=bucket, Key=pre + ".changecounter", Body=str(seq).encode())
    for frame, r in enumerate(records, 1):
        rec = {"frame": frame, "seq": r["seq"], "id": r["id"], "payload": r["payload"]}
        body = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        ts = int(created) + 5 * frame
        s3.put_object(Bucket=bucket, Key=pre + f"{frame:012d}-{frame:012d}-{ts}.jsonl", Body=body)


def history_records(pairs):
    """[(seq, id), ...] -> records with the verifier's payloads."""
    return [{"seq": s, "id": i, "payload": payload(i)} for s, i in pairs]
