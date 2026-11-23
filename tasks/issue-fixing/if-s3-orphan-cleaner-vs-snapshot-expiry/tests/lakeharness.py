"""Verifier harness: table model + writer, and an S3 gateway that forwards the cleaner's requests
to Floci, refuses its writes, audits each of its deletes when it is forwarded, and lets a
scenario run commits and expiry while one of its requests is held.

Nothing here imports /app.
"""

import http.client
import json
import random
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

FLOCI_HOST = "127.0.0.1"
FLOCI_PORT = 4566
FLOCI_URL = f"http://localhost:{FLOCI_PORT}"
RETAIN_MAX = 2

HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer",
              "upgrade", "expect", "proxy-authorization", "proxy-authenticate"}


def direct_client():
    return boto3.client(
        "s3", endpoint_url=FLOCI_URL, region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 4, "mode": "standard"},
                      connect_timeout=5, read_timeout=30, max_pool_connections=16),
    )


def not_found(error):
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


# --------------------------------------------------------------------------------------------
# table model and writer


class Table:
    """Ground truth for one table prefix. Every mutation goes to S3 and to the model together."""

    def __init__(self, s3, bucket, prefix, rng):
        self.s3, self.bucket, self.prefix, self.rng = s3, bucket, prefix, rng
        self.lists = {}       # list name -> manifest names
        self.manifests = {}   # manifest name -> data paths
        self.snapshots = {}   # id -> (base list, delta list)
        self.present = set()  # ids whose snapshot JSON exists
        self.latest = None
        self.removed = set()  # paths deleted at setup although a snapshot references them
        self.orphans = set()  # pre-cutoff paths no snapshot references
        self.marks = {}       # named objects for scenarios

    # -- raw S3 --
    def put(self, path, body):
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + path, Body=body)

    def delete(self, path):
        self.s3.delete_object(Bucket=self.bucket, Key=self.prefix + path)

    def exists(self, path):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.prefix + path)
            return True
        except ClientError as e:
            if not_found(e):
                return False
            raise

    # -- objects --
    def new_data(self, n):
        paths = []
        for _ in range(n):
            path = f"bucket-0/data-{uuid.uuid4()}.parquet"
            self.put(path, b"PAR1" + self.rng.randbytes(self.rng.randint(16, 96)) + b"PAR1")
            paths.append(path)
        return paths

    def new_manifest(self, files):
        name = f"manifest-{uuid.uuid4()}"
        self.put("manifest/" + name, json.dumps({"files": list(files)}).encode())
        self.manifests[name] = list(files)
        return name

    def new_list(self, manifests):
        name = f"manifest-list-{uuid.uuid4()}"
        self.put("manifest/" + name, json.dumps({"manifests": list(manifests)}).encode())
        self.lists[name] = list(manifests)
        return name

    def new_snapshot(self, base, delta, kind):
        sid = (self.latest or 0) + 1
        base_list, delta_list = self.new_list(base), self.new_list(delta)
        body = {"id": sid, "baseManifestList": base_list, "deltaManifestList": delta_list,
                "commitKind": kind, "timeMillis": int(time.time() * 1000)}
        self.put(f"snapshot/snapshot-{sid}", json.dumps(body).encode())
        self.snapshots[sid] = (base_list, delta_list)
        self.present.add(sid)
        self.latest = sid
        self.put("snapshot/LATEST", str(sid).encode())
        return sid

    def set_earliest(self, sid):
        self.put("snapshot/EARLIEST", str(sid).encode())

    # -- references --
    def list_names(self, sid):
        return set(self.snapshots[sid])

    def manifest_names(self, sid):
        return {m for name in self.snapshots[sid] for m in self.lists[name]}

    def data_paths(self, sid):
        return {f for m in self.manifest_names(sid) for f in self.manifests[m]}

    def refs(self, sid):
        return ({"manifest/" + n for n in self.list_names(sid)}
                | {"manifest/" + n for n in self.manifest_names(sid)}
                | self.data_paths(sid))

    def live_refs(self):
        out = set()
        for sid in self.present:
            out |= self.refs(sid)
        return out

    # -- commits --
    def commit_append(self, merge=False):
        base_list, delta_list = self.snapshots[self.latest]
        base = self.lists[base_list] + self.lists[delta_list]
        if merge:
            base = [self.new_manifest(sorted({f for m in base for f in self.manifests[m]}))]
        delta = [self.new_manifest(self.new_data(1))]
        return self.new_snapshot(base, delta, "APPEND")

    def commit_compact(self):
        base = [self.new_manifest(self.new_data(1))]
        return self.new_snapshot(base, [], "COMPACT")

    # -- expiry, split at the point where the snapshot files are deleted --
    def expire_files(self, drop_ids):
        """Steps 1-2 of expiry for `drop_ids` (data, then manifests and lists)."""
        keep_ids = self.present - set(drop_ids)
        keep_data, keep_manifests = set(), set()
        for sid in keep_ids:
            keep_data |= self.data_paths(sid)
            keep_manifests |= self.manifest_names(sid)
        drop_data, drop_manifests, drop_lists = set(), set(), set()
        for sid in drop_ids:
            drop_data |= self.data_paths(sid)
            drop_manifests |= self.manifest_names(sid)
            drop_lists |= self.list_names(sid)
        for path in sorted(drop_data - keep_data):
            self.delete(path)
        for name in sorted(drop_manifests - keep_manifests):
            self.delete("manifest/" + name)
        for name in sorted(drop_lists):
            self.delete("manifest/" + name)

    def expire_snapshots(self, drop_ids):
        """Steps 3-4: the snapshot files, then EARLIEST."""
        for sid in sorted(drop_ids):
            self.delete(f"snapshot/snapshot-{sid}")
            self.present.discard(sid)
        self.set_earliest(min(self.present))

    def retention_victims(self):
        ordered = sorted(self.present)
        return ordered[:-RETAIN_MAX] if len(ordered) > RETAIN_MAX else []

    # -- the starting state every scenario shares --
    def build_history(self):
        """S1: base [m1..mM]; mM holds files only S1 uses plus y. S2: compaction of S1 that drops
        S1's own files and adds an old compaction output c next to y in m9."""
        n_manifests = self.rng.randint(6, 9)
        per_manifest = self.rng.randint(3, 6)
        shared = [self.new_manifest(self.new_data(per_manifest)) for _ in range(n_manifests - 1)]
        s1_only = self.new_data(per_manifest - 1)
        y = self.new_data(1)[0]
        last = self.new_manifest(s1_only + [y])
        self.new_snapshot(shared + [last], [], "APPEND")
        self.set_earliest(1)
        c = self.new_data(1)[0]
        m9 = self.new_manifest([y, c])
        self.new_snapshot(shared + [m9], [], "COMPACT")
        self.marks.update(shared=shared, s1_last=last, s1_only=s1_only, y=y, c=c, m9=m9)

    def add_orphans(self):
        """What killed writers leave behind: data files, and a manifest + list naming some."""
        data = self.new_data(self.rng.randint(6, 12))
        manifest = self.new_manifest(data[:2])
        lst = self.new_list([manifest])
        stray = self.new_manifest(data[2:3])
        self.orphans |= set(data) | {"manifest/" + manifest, "manifest/" + lst, "manifest/" + stray}
        # orphans are not part of the snapshot model
        for name in (manifest, stray):
            self.manifests.pop(name, None)
        self.lists.pop(lst, None)

    def remove(self, path):
        """Genuine loss: delete a file a retained snapshot references."""
        self.delete(path)
        self.removed.add(path)


# --------------------------------------------------------------------------------------------
# gateway


@dataclass
class Request:
    method: str
    key: Optional[str]          # path relative to the active table prefix, if under it
    list_prefix: Optional[str]  # for listings: requested prefix relative to the table prefix
    full_key: Optional[str]

    @property
    def is_list(self):
        return self.list_prefix is not None

    def lists_snapshot_dir(self):
        return self.is_list and ("snapshot/".startswith(self.list_prefix) or self.list_prefix.startswith("snapshot/"))

    def enumerates_snapshots(self):
        """A listing of snapshot/ or a read of one of the hints: how a cleaner finds snapshots."""
        return self.lists_snapshot_dir() or (self.method == "GET" and self.key in ("snapshot/LATEST", "snapshot/EARLIEST"))

    def reads_snapshot_file(self):
        return self.method == "GET" and bool(self.key) and self.key.startswith("snapshot/snapshot-")


@dataclass
class Run:
    table: Table
    scenario: object
    lock: threading.RLock
    deletes: list = field(default_factory=list)        # (relative key or full key, existed)
    violations: list = field(default_factory=list)
    refused_writes: list = field(default_factory=list)
    read_snapshot_file: bool = False
    gets: int = 0
    tails: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def after_delay(self, seconds, action):
        def body():
            time.sleep(seconds)
            try:
                with self.lock:
                    action()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"tail action failed: {e!r}")
        t = threading.Thread(target=body, daemon=True)
        t.start()
        self.tails.append(t)

    def wait_tails(self, timeout=15):
        end = time.monotonic() + timeout
        for t in self.tails:
            t.join(max(0.0, end - time.monotonic()))


class Gateway:
    def __init__(self, bucket, port):
        self.bucket = bucket
        self.port = port
        self.lock = threading.RLock()
        self.run = None
        handler = type("Handler", (_Handler,), {"gateway": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()

    def begin(self, table, scenario):
        with self.lock:
            self.run = Run(table=table, scenario=scenario, lock=self.lock)
        return self.run

    def end(self):
        with self.lock:
            run, self.run = self.run, None
        return run

    # -- request handling --
    def classify(self, method, path, query):
        parts = unquote(path).lstrip("/").split("/", 1)
        bucket = parts[0]
        key = parts[1] if len(parts) > 1 and parts[1] else None
        run = self.run
        prefix = run.table.prefix if run else None
        rel = key[len(prefix):] if (key and prefix and key.startswith(prefix)) else None
        list_prefix = None
        if method == "GET" and key is None and bucket and ("list-type" in query or "prefix" in query):
            p = query.get("prefix", [""])[0]
            if prefix is not None and (p.startswith(prefix) or prefix.startswith(p)):
                list_prefix = p[len(prefix):] if p.startswith(prefix) else ""
        return bucket, key, Request(method=method, key=rel, list_prefix=list_prefix, full_key=key)

    def audit_delete(self, run, full_key):
        table = run.table
        rel = full_key[len(table.prefix):] if full_key.startswith(table.prefix) else None
        existed = self._exists_direct(table, full_key)
        run.deletes.append((rel if rel is not None else full_key, existed))
        if not existed:
            return
        if rel is None:
            run.violations.append(f"deleted {full_key} outside the table")
        elif rel.startswith("snapshot/"):
            run.violations.append(f"deleted {rel}")
        elif rel in table.live_refs():
            users = sorted(s for s in table.present if rel in table.refs(s))
            run.violations.append(f"deleted {rel} while snapshot(s) {users} referencing it exist")

    def _exists_direct(self, table, full_key):
        try:
            table.s3.head_object(Bucket=self.bucket, Key=full_key)
            return True
        except ClientError as e:
            if not_found(e):
                return False
            raise


def _forward(method, raw_path, headers, body):
    conn = http.client.HTTPConnection(FLOCI_HOST, FLOCI_PORT, timeout=60)
    try:
        conn.request(method, raw_path, body=body if body else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.getheaders(), data
    finally:
        conn.close()


DENIED = (b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code>'
          b'<Message>Access Denied: the maintenance role cannot write</Message></Error>')


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            return self.rfile.read(length)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return b""

    def _handle(self):
        gw = self.gateway
        method = self.command
        body = self._read_body()
        split = urlsplit(self.path)
        query = parse_qs(split.query, keep_blank_values=True)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        if body and not any(k.lower() == "content-length" for k in headers):
            headers["Content-Length"] = str(len(body))
        with gw.lock:
            run = gw.run
            bucket, key, req = gw.classify(method, split.path, query)
            write = method == "PUT" or (method == "POST" and "delete" not in query)
            if run is not None and write:
                run.refused_writes.append(f"{method} {self.path}")
                return self._reply(403, [("Content-Type", "application/xml")], DENIED)
            if run is not None:
                try:
                    if method == "DELETE" and key:
                        gw.audit_delete(run, key)
                    elif method == "POST" and "delete" in query:
                        for k in _delete_keys(body):
                            gw.audit_delete(run, k)
                    if req.reads_snapshot_file():
                        run.read_snapshot_file = True
                    if method == "GET" and req.key:
                        run.gets += 1
                    run.scenario.before(run, req)
                except Exception as e:  # noqa: BLE001
                    run.errors.append(f"harness before-hook failed: {e!r}")
            status, resp_headers, data = _forward(method, self.path, headers, body)
            if run is not None:
                try:
                    run.scenario.after(run, req, status)
                except Exception as e:  # noqa: BLE001
                    run.errors.append(f"harness after-hook failed: {e!r}")
        self._reply(status, resp_headers, data)

    def _reply(self, status, headers, data):
        self.send_response_only(status)
        upstream_length = None
        for k, v in headers:
            lk = k.lower()
            if lk in HOP_BY_HOP:
                continue
            if lk == "content-length":
                upstream_length = v
                continue
            self.send_header(k, v)
        if self.command == "HEAD":
            self.send_header("Content-Length", upstream_length or "0")
            self.end_headers()
        else:
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        self.wfile.flush()


def _delete_keys(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    keys = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "Key" and el.text:
            keys.append(el.text)
    return keys


# --------------------------------------------------------------------------------------------
# scenarios: setup on the table, then hooks the gateway calls under its lock


class Scenario:
    expect = "clean"  # or "abort"

    def setup(self, table):
        pass

    def before(self, run, req):
        pass

    def after(self, run, req, status):
        pass


class Static(Scenario):
    pass


class ListThenExpire(Scenario):
    """#7710: whatever the cleaner first learns about the snapshots (a listing, or a hint) is
    stale by the time it reads anything: two commits land and expire every snapshot it saw."""

    def __init__(self):
        self.fired = False

    def after(self, run, req, status):
        if self.fired or not req.enumerates_snapshots():
            return
        self.fired = True
        t = run.table
        for _ in range(2):
            t.commit_append()
            victims = t.retention_victims()
            t.expire_files(victims)
            t.expire_snapshots(victims)


class ReadThenExpire(ListThenExpire):
    """Same two commits and complete expiry, but later: on the first look at the snapshots
    (a listing, or a hint) that follows a read of a snapshot file. A cleaner that never looks
    again is not disturbed."""

    def after(self, run, req, status):
        if run.read_snapshot_file:
            super().after(run, req, status)


class MergeThenExpire(Scenario):
    """A merge commit and an append land while a read of snapshot 2 is held; expiry removes
    S1/S2's manifests and lists, the held read 404s, and the snapshot files go `tail` later."""

    def __init__(self, trigger, tail):
        self.trigger, self.tail, self.fired = trigger, tail, False

    def _targets(self, t):
        if self.trigger == "list":
            return {"manifest/" + n for n in t.list_names(2)}
        return {"manifest/" + t.marks["m9"]}

    def before(self, run, req):
        t = run.table
        if self.fired or req.method != "GET" or req.key not in self._targets(t):
            return
        self.fired = True
        t.commit_append(merge=True)
        t.commit_append()
        self.victims = t.retention_victims()
        t.expire_files(self.victims)

    def after(self, run, req, status):
        if self.fired and not getattr(self, "scheduled", False):
            self.scheduled = True
            t = run.table
            run.after_delay(self.tail / 1000.0, lambda: t.expire_snapshots(self.victims))


class LostWhileCommitting(Scenario):
    """Files of snapshot 2 are genuinely gone. While the first read of one is held, a full
    compaction and an append land; their expiry is deferred, and nothing changes afterwards."""

    expect = "abort"

    def __init__(self, what):
        self.what, self.fired = what, False

    def setup(self, table):
        base, delta = table.snapshots[2]
        if self.what == "lists":
            self.targets = {"manifest/" + base, "manifest/" + delta}
        elif self.what == "base":
            self.targets = {"manifest/" + base}
        else:
            self.targets = {"manifest/" + table.marks["m9"]}
        for path in sorted(self.targets):
            table.remove(path)

    def before(self, run, req):
        if self.fired or req.method != "GET" or req.key not in self.targets:
            return
        self.fired = True
        run.table.commit_compact()
        run.table.commit_append()


class Missing(Scenario):
    """A file both retained snapshots need is gone; nobody is committing."""

    expect = "abort"

    def __init__(self, what):
        self.what = what

    def setup(self, table):
        if self.what == "shared":
            path = "manifest/" + table.marks["shared"][0]
        elif self.what == "s1-base":
            path = "manifest/" + table.snapshots[1][0]
        else:
            path = "manifest/" + table.marks["shared"][-1]
        table.remove(path)


class Churn(Scenario):
    """Every read of the current latest snapshot file lands a new commit and expires the oldest
    retained snapshot (snapshot files `tail` later)."""

    CAP = 400

    def __init__(self, tail):
        self.tail, self.commits, self.pending = tail, 0, None

    def before(self, run, req):
        t = run.table
        if req.method != "GET" or req.key != f"snapshot/snapshot-{t.latest}" or self.commits >= self.CAP:
            return
        self.commits += 1
        t.commit_append()
        victims = [v for v in t.retention_victims() if v in t.present]
        t.expire_files(victims)
        self.pending = victims

    def after(self, run, req, status):
        if self.pending is not None:
            victims, self.pending = self.pending, None
            t = run.table
            if victims:
                run.after_delay(self.tail / 1000.0, lambda: t.expire_snapshots(victims))


class Empty(Scenario):
    """A table whose first commit never landed: only orphans."""
