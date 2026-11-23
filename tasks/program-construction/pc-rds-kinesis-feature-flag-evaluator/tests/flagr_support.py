"""Shared helpers: run the candidate server, call its REST API, read its Postgres
database and Kinesis stream, and compare JSON under the public observation rules."""

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
import psycopg

from harness import start_command, workspace

PG_USER = "flagr"
PG_PASSWORD = "flagr-pass-1"
KPL_MAGIC = b"\xf3\x89\x9a\xc2"


# --------------------------------------------------------------------------- JSON rules

# Optional numeric/boolean fields that the reference omits when zero/false.
OMIT_FALSY = {"maxID", "flagID", "segmentID", "variantID", "flagSnapshotID", "dataRecordsEnabled", "enableDebug"}
VOLATILE = {"timestamp", "updatedAt"}
OPAQUE = {"entityContext", "attachment", "variantAttachment"}
RANDOM_ENTITY = re.compile(r"^\S+$")  # generated entity IDs: any non-empty token


def _empty(value):
    return value is None or (type(value) in (str, list, dict) and len(value) == 0)


def canon(value, *, volatile=VOLATILE, _context=False):
    """Normalize a decoded JSON document: drop empty optional fields and volatile
    timestamps, reduce debug messages to their presence."""
    if isinstance(value, list):
        return [canon(v, volatile=volatile) for v in value]
    if not isinstance(value, dict):
        return value
    # Zero/false optional fields only exist on evaluation results, their contexts and maxID.
    falsy_optional = _context or "recordSource" in value or "evalContext" in value or "maxID" in value
    result = {}
    for key, item in value.items():
        if key in volatile:
            continue
        if key == "evalDebugLog" and isinstance(item, dict):
            item = _debug_shape(item)
        elif key in OPAQUE:
            pass  # caller-supplied data and attachments are compared as given
        else:
            item = canon(item, volatile=volatile, _context=key == "evalContext")
        if _empty(item):
            continue
        if falsy_optional and key in OMIT_FALSY and type(item) in (bool, int, float) and not item:
            continue
        result[key] = item
    return result


def _debug_shape(log):
    shaped = {}
    if log.get("msg"):
        shaped["msg"] = "<text>"
    segs = log.get("segmentDebugLogs") or []
    if segs:
        shaped["segmentDebugLogs"] = [
            {k: v for k, v in (("segmentID", s.get("segmentID")), ("msg", "<text>" if s.get("msg") else None)) if v}
            for s in segs
        ]
    return shaped


def same(actual, expected):
    """Strict structural equality: booleans are not numbers, numbers compare by value."""
    if type(expected) is bool or type(actual) is bool:
        return type(actual) is type(expected) and actual == expected
    if type(expected) in (int, float):
        return type(actual) in (int, float) and actual == expected
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(same(actual[k], expected[k]) for k in expected)
    if isinstance(expected, (list, tuple)):
        return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(same(a, e) for a, e in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def assert_doc(actual, expected, *, volatile=VOLATILE, what="document"):
    a, e = canon(actual, volatile=volatile), canon(expected, volatile=volatile)
    assert same(a, e), f"{what} mismatch\n actual:   {json.dumps(a, sort_keys=True)}\n expected: {json.dumps(e, sort_keys=True)}"


def assert_unordered(actual, expected, *, volatile=VOLATILE, what="collection"):
    """Compare lists of documents ignoring order but keeping multiplicity."""
    rest = [canon(a, volatile=volatile) for a in actual]
    for item in expected:
        want = canon(item, volatile=volatile)
        match = next((i for i, a in enumerate(rest) if same(a, want)), None)
        assert match is not None, f"{what}: missing {json.dumps(want, sort_keys=True)}\n in {json.dumps(rest, sort_keys=True)}"
        rest.pop(match)
    assert not rest, f"{what}: unexpected extra items {json.dumps(rest, sort_keys=True)}"


def assert_timestamped(result):
    assert isinstance(result.get("timestamp"), str) and result["timestamp"], result


def assert_rows(actual, expected, what="rows"):
    assert same([list(r) for r in actual], [list(r) for r in expected]), f"{what} mismatch\n actual:   {actual}\n expected: {expected}"


# --------------------------------------------------------------------------- Kinesis


def _varint(buf, i):
    shift = value = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, i


def _fields(buf):
    i, out = 0, []
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            size, i = _varint(buf, i)
            if i + size > len(buf):
                raise ValueError("truncated field")
            value, i = buf[i:i + size], i + size
        else:
            raise ValueError(f"unexpected wire type {wire}")
        out.append((number, wire, value))
    return out


class KinesisRecord:
    """One Kinesis record; `aggregated` records are decoded from the KPL format."""

    def __init__(self, partition_key, data):
        self.partition_key = partition_key
        self.data = data
        self.aggregated = data.startswith(KPL_MAGIC)
        self.key_table, self.hash_key_table, self.entries = [], [], []
        if not self.aggregated:
            self.entries = [(None, data)]
            return
        body, digest = data[4:-16], data[-16:]
        assert len(data) >= 20 and hashlib.md5(body).digest() == digest, "aggregated record has a bad MD5 trailer"
        for number, wire, value in _fields(body):
            if number == 1 and wire == 2:
                self.key_table.append(value.decode())
            elif number == 2 and wire == 2:
                self.hash_key_table.append(value.decode())
            elif number == 3 and wire == 2:
                parts = {}
                for n2, w2, v2 in _fields(value):
                    parts.setdefault((n2, w2), []).append(v2)
                index = parts.get((1, 0), [None])[0]
                (payload,) = parts[(3, 2)]
                self.entries.append((index, payload))
            else:
                raise AssertionError(f"unexpected AggregatedRecord field {number}/{wire}")

    @property
    def frames(self):
        return [json.loads(raw.decode()) for _, raw in self.entries]


def string_payload(frame):
    """A payload_string frame: {"payload": "<json>", "encrypted": false}."""
    assert set(frame) == {"payload", "encrypted"}, frame
    assert frame["encrypted"] is False, frame
    assert isinstance(frame["payload"], str), frame
    return json.loads(frame["payload"])


def raw_payload(frame):
    """A payload_raw_json frame: {"payload": {...}}."""
    assert set(frame) == {"payload"}, frame
    assert isinstance(frame["payload"], dict), frame
    return frame["payload"]


# --------------------------------------------------------------------------- server


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        try:
            self.json = json.loads(body) if body else None
        except ValueError:
            self.json = None

    def __repr__(self):
        return f"<{self.status} {self.body[:400]!r}>"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A candidate server on its own database (and Kinesis stream, if recording)."""

    def __init__(self, postgres, *, env=None, recorder=True, refresh="250ms", database=None):
        self.postgres = postgres
        self.recorder = recorder
        self.extra_env = dict(env or {})
        self.refresh = refresh
        self.database = database
        self.owns_database = database is None
        self.stream = None
        self.process = None
        self._sync = 0
        self.kinesis = boto3.client("kinesis")
        self._iterators = None
        self._log_dir = tempfile.mkdtemp(prefix="flagr-log-")
        self._workspace = None

    # lifecycle
    def start(self):
        if self.database is None:
            self.database = "t_" + uuid.uuid4().hex[:12]
            with self.admin() as c:
                c.execute(f'CREATE DATABASE "{self.database}"')
        if self.recorder and self.stream is None:
            self.stream = "flagr-" + uuid.uuid4().hex[:12]
            self.kinesis.create_stream(StreamName=self.stream, ShardCount=1)
            self.kinesis.get_waiter("stream_exists").wait(StreamName=self.stream, WaiterConfig={"Delay": 1, "MaxAttempts": 60})
        self.launch()
        return self

    def environment(self):
        env = {
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "FLAGR_UI_ENABLED": "false",
            "FLAGR_DB_DBDRIVER": "postgres",
            "FLAGR_DB_DBCONNECTIONSTR": self.dsn,
            "FLAGR_DB_DBCONNECTION_DEBUG": "false",
            "FLAGR_EVALCACHE_REFRESHINTERVAL": self.refresh,
        }
        if self.recorder:
            env.update({
                "FLAGR_RECORDER_ENABLED": "true",
                "FLAGR_RECORDER_TYPE": "kinesis",
                "FLAGR_RECORDER_KINESIS_STREAM_NAME": self.stream,
                "FLAGR_RECORDER_KINESIS_FLUSH_INTERVAL": "300ms",
                "FLAGR_RECORDER_KINESIS_MAX_CONNECTIONS": "1",
            })
        env.update(self.extra_env)
        return env

    def launch(self, args=()):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}/api/v1"
        self._workspace = workspace()
        cwd = self._workspace.__enter__()
        self.log_path = os.path.join(self._log_dir, f"server-{self.port}.log")
        self._log = open(self.log_path, "wb")  # noqa: SIM115 -- the child writes it until stop()
        self.process = start_command(["/app/program", *args], cwd=cwd, env=self.environment(), stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"server exited with {self.process.returncode} before becoming healthy\n{self.log_tail()}")
            try:
                if self.call("GET", "/health", timeout=2).status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError(f"server never answered GET /api/v1/health\n{self.log_tail()}")

    def stop(self):
        """SIGTERM the server; returns its exit status."""
        status = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            status = self.process.returncode
            self.process = None
            self._log.close()
            self._workspace.__exit__(None, None, None)
        return status

    def close(self):
        self.stop()
        if self.stream:
            try:
                self.kinesis.delete_stream(StreamName=self.stream, EnforceConsumerDeletion=True)
            except Exception:  # noqa: BLE001 -- best-effort cleanup
                pass
        if self.owns_database and self.database:
            try:
                with self.admin() as c:
                    c.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
            except Exception:  # noqa: BLE001
                pass

    def log_tail(self, size=3000):
        try:
            with open(self.log_path, "rb") as f:
                return f.read()[-size:].decode(errors="replace")
        except OSError:
            return ""

    # postgres
    def admin(self):
        p = self.postgres
        return psycopg.connect(host=p["host"], port=p["port"], user=PG_USER, password=PG_PASSWORD, dbname="postgres",
                               connect_timeout=10, autocommit=True)

    @property
    def dsn(self):
        p = self.postgres
        return f"postgres://{PG_USER}:{PG_PASSWORD}@{p['host']}:{p['port']}/{self.database}?sslmode=disable"

    def rows(self, query, args=()):
        p = self.postgres
        with psycopg.connect(host=p["host"], port=p["port"], user=PG_USER, password=PG_PASSWORD, dbname=self.database,
                             connect_timeout=10, autocommit=True) as c:
            return c.execute(query, args).fetchall()

    # http
    def call(self, method, path, body=None, *, query=None, raw=None, timeout=15):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                return Response(r.status, r.read().decode(errors="replace"))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read().decode(errors="replace"))

    def ok(self, method, path, body=None, **kw):
        r = self.call(method, path, body, **kw)
        assert r.status == 200, f"{method} {path} {body} -> {r}"
        return r.json

    def evaluate(self, context):
        return self.ok("POST", "/evaluation", context)

    def batch(self, request):
        return self.ok("POST", "/evaluation/batch", request)["evaluationResults"]

    def sync(self, timeout=20):
        """Create a disabled marker flag and wait until evaluation sees it; the
        evaluation view then reflects every earlier write. Markers never record."""
        self._sync += 1
        key = f"zz-sync-{self._sync}"
        self.ok("POST", "/flags", {"description": "sync marker", "key": key})
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.evaluate({"flagKey": key, "entityID": "sync"})
            if r.get("flagID"):
                return
            time.sleep(0.1)
        raise AssertionError("a newly created flag never became visible to evaluation")

    # building blocks
    def flag(self, key, description=None, variants=(), enabled=None, records=None, **extra):
        f = self.ok("POST", "/flags", {"description": description or key, "key": key})
        out = {"id": f["id"], "variants": {}}
        for v in variants:
            name, attachment = (v, None) if isinstance(v, str) else v
            body = {"key": name} if attachment is None else {"key": name, "attachment": attachment}
            out["variants"][name] = self.ok("POST", f"/flags/{f['id']}/variants", body)["id"]
        if records is not None or extra:
            body = dict(extra)
            if records is not None:
                body["dataRecordsEnabled"] = records
            self.ok("PUT", f"/flags/{f['id']}", body)
        if enabled is not None:
            self.ok("PUT", f"/flags/{f['id']}/enabled", {"enabled": enabled})
        return out

    def segment(self, flag, description, rollout, distribution=(), constraints=()):
        fid = flag["id"]
        s = self.ok("POST", f"/flags/{fid}/segments", {"description": description, "rolloutPercent": rollout})
        for prop, op, value in constraints:
            self.ok("POST", f"/flags/{fid}/segments/{s['id']}/constraints", {"property": prop, "operator": op, "value": value})
        if distribution:
            self.ok("PUT", f"/flags/{fid}/segments/{s['id']}/distributions", {"distributions": [
                {"variantID": flag["variants"][k], "variantKey": k, "percent": p} for k, p in distribution]})
        return s["id"]

    # kinesis
    def records(self, until, timeout=25, settle=1.0):
        """Read the stream from its start until `until(records)` holds, then keep reading
        for `settle` seconds so that extra records are observed as well."""
        shards = [s["ShardId"] for s in self.kinesis.describe_stream(StreamName=self.stream)["StreamDescription"]["Shards"]]
        if self._iterators is None:
            self._iterators = {s: self.kinesis.get_shard_iterator(StreamName=self.stream, ShardId=s, ShardIteratorType="TRIM_HORIZON")["ShardIterator"] for s in shards}
            self._seen = []
        deadline = time.time() + timeout
        satisfied_at = None
        while True:
            for shard in shards:
                out = self.kinesis.get_records(ShardIterator=self._iterators[shard], Limit=1000)
                self._iterators[shard] = out["NextShardIterator"]
                self._seen += [KinesisRecord(r["PartitionKey"], r["Data"]) for r in out["Records"]]
            now = time.time()
            if satisfied_at is None and until(self._seen):
                satisfied_at = now
            if satisfied_at is not None and now - satisfied_at >= settle:
                return list(self._seen)
            if now > deadline:
                return list(self._seen)
            time.sleep(0.25)


def frame_count(records):
    return sum(len(r.entries) for r in records)
