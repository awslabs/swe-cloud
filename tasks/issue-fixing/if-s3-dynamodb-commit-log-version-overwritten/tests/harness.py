"""Verifier-side harness: workers under driver control, a reclaimer, and
direct observation of the two services. Independent of /app code."""

import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
APP = "/app"
WORKER = "/app/worker.py"

# Schedule bounds (seconds).
RETENTION = 2          # completed entries expire this long after completion
TOLERANCE = 1          # integer-second rounding allowance on expiry values
GRACE = 3              # extra time allowed for the reclaimer to act
STALE_HOLD = 6         # how long the crashed writer's entry must outlive it
OP_TIMEOUT = 10        # bound on any released worker operation
BARRIER_TIMEOUT = 15   # bound on reaching a driver handshake or startup
POLL = 0.05            # observation cadence

FILE_NAME_RE = re.compile(r"^(\d{20})\.json$")


def file_name(version):
    return "%020d.json" % version


def aws_client(service):
    return boto3.client(
        service,
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        config=Config(retries={"max_attempts": 2}, connect_timeout=5, read_timeout=15),
    )


def wait_until(predicate, timeout, poll=POLL):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


class OpsListener:
    """Drives the operations socket the store announces on.

    Every announcement is answered with a release unless it matches an active
    hold; a held announcement parks the announcing process until the hold is
    released (or its process is killed).
    """

    def __init__(self):
        self.path = os.path.join(tempfile.gettempdir(), "ops-%s.sock" % uuid.uuid4().hex)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(128)
        self.server.settimeout(0.2)
        self.announcements = []
        self._cond = threading.Condition()
        self._holds = {}        # hid -> {"match": {...}, "conns": [...]}
        self._next_hid = [0]
        self._stopped = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stopped:
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(120)
        buf = b""
        while b"\n" not in buf:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
        try:
            facts = json.loads(buf.split(b"\n", 1)[0].decode())
        except ValueError:
            facts = {"event": "unparsable"}
        with self._cond:
            self.announcements.append(facts)
            hold = None
            for hid, entry in self._holds.items():
                if entry["released"]:
                    continue
                if self._matches(facts, entry["match"]):
                    entry["conns"].append(conn)
                    hold = hid
                    break
            self._cond.notify_all()
        if hold is None:
            try:
                conn.sendall(b"go\n")
            except OSError:
                pass

    @staticmethod
    def _matches(facts, match):
        return all(facts.get(k) == v for k, v in match.items())

    def hold(self, pid, event, file=None, key=None):
        """Park the next announcement matching (pid, event[, file|key])."""
        match = {"pid": pid, "event": event}
        if file is not None:
            match["file"] = file
        if key is not None:
            match["key"] = key
        with self._cond:
            self._next_hid[0] += 1
            hid = self._next_hid[0]
            self._holds[hid] = {"match": match, "conns": [], "released": False}
        return hid

    def release(self, hid):
        with self._cond:
            entry = self._holds.get(hid)
            if entry is None or entry["released"]:
                return
            entry["released"] = True
            conns = entry["conns"]
            entry["conns"] = []
        for conn in conns:
            try:
                conn.sendall(b"go\n")
            except OSError:
                pass

    def wait_for(self, predicate, timeout=BARRIER_TIMEOUT):
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for facts in self.announcements:
                    if predicate(facts):
                        return facts
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def stop(self):
        self._stopped = True
        try:
            self.server.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Worker:
    """One store process in its own working directory, announced to the driver."""

    def __init__(self, listener, bucket, log_table, table_path, name="", announce=True):
        self.name = name or "worker"
        self.cwd = tempfile.mkdtemp(prefix="worker-")
        env = dict(os.environ)
        env.update(
            PYTHONPATH=APP,
            BUCKET=bucket,
            LOG_TABLE=log_table,
            TABLE_PATH=table_path,
        )
        if announce:
            env["SEAM_SOCKET"] = listener.path
        self.proc = subprocess.Popen(
            [sys.executable, WORKER],
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.pid = self.proc.pid
        self._lines = queue.Queue()
        self._stderr = []
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self):
        try:
            for line in self.proc.stdout:
                self._lines.put(line.strip())
        except OSError:
            pass

    def _drain_stderr(self):
        try:
            for line in self.proc.stderr:
                if len(self._stderr) < 2000:
                    self._stderr.append(line.rstrip())
        except OSError:
            pass

    def send(self, op, **fields):
        command = {"op": op}
        command.update(fields)
        try:
            self.proc.stdin.write(json.dumps(command) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def recv(self, timeout=OP_TIMEOUT):
        try:
            return json.loads(self._lines.get(timeout=timeout))
        except (queue.Empty, ValueError):
            return None

    def call(self, op, timeout=OP_TIMEOUT, **fields):
        self.send(op, **fields)
        return self.recv(timeout)

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        if self.alive():
            try:
                self.proc.kill()
            except OSError:
                pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class Sweeper(threading.Thread):
    """The deployment's reclaimer: removes completed entries past their expiry.

    It never touches an entry without an expiry, and never writes log objects.
    """

    def __init__(self, ddb, log_table):
        super().__init__(daemon=True)
        self.ddb = ddb
        self.log_table = log_table
        self.prefixes = set()
        self.removed = []
        self._halt = threading.Event()
        self._lock = threading.Lock()

    def register(self, table_path):
        with self._lock:
            self.prefixes.add(table_path)

    def run(self):
        while not self._halt.is_set():
            with self._lock:
                prefixes = list(self.prefixes)
            for table_path in prefixes:
                for row in query_rows(self.ddb, self.log_table, table_path):
                    if not row["complete"] or row["expireTime"] is None:
                        continue
                    if row["expireTime"] > time.time():
                        continue
                    try:
                        self.ddb.delete_item(
                            TableName=self.log_table,
                            Key={
                                "tablePath": {"S": table_path},
                                "fileName": {"S": row["fileName"]},
                            },
                            ConditionExpression="#c = :c AND #e <= :n",
                            ExpressionAttributeNames={"#c": "complete", "#e": "expireTime"},
                            ExpressionAttributeValues={
                                ":c": {"BOOL": True},
                                ":n": {"N": str(int(time.time()))},
                            },
                        )
                        self.removed.append((table_path, row["fileName"]))
                    except ClientError as exc:
                        code = exc.response.get("Error", {}).get("Code", "")
                        if code != "ConditionalCheckFailedException":
                            raise
            self._halt.wait(POLL)

    def halt(self):
        self._halt.set()
        self.join(timeout=5)


def query_rows(ddb, log_table, table_path):
    """Every coordination entry for one table, consistently read."""
    rows = []
    kwargs = {
        "TableName": log_table,
        "KeyConditionExpression": "tablePath = :t",
        "ExpressionAttributeValues": {":t": {"S": table_path}},
        "ConsistentRead": True,
    }
    while True:
        page = ddb.query(**kwargs)
        for item in page.get("Items", []):
            expire = item.get("expireTime")
            rows.append(
                {
                    "tablePath": item.get("tablePath", {}).get("S", ""),
                    "fileName": item.get("fileName", {}).get("S", ""),
                    "tempPath": item.get("tempPath", {}).get("S", ""),
                    "complete": bool(item.get("complete", {}).get("BOOL", False)),
                    "expireTime": int(expire["N"]) if expire and "N" in expire else None,
                }
            )
        if not page.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_raw_item(ddb, log_table, table_path, file):
    return ddb.get_item(
        TableName=log_table,
        Key={"tablePath": {"S": table_path}, "fileName": {"S": file}},
        ConsistentRead=True,
    ).get("Item")


def s3_read(s3, bucket, key):
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError:
        return None


def s3_listing(s3, bucket, prefix):
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys.extend(item["Key"] for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def final_versions(s3, bucket, table_path):
    prefix = "%s/log/" % table_path
    found = {}
    for key in s3_listing(s3, bucket, prefix):
        name = key.rsplit("/", 1)[-1]
        match = FILE_NAME_RE.match(name)
        if match:
            found[int(match.group(1))] = key
    return found


def check(failures, condition, message):
    if not condition:
        failures.append(message)
    return condition


def status_of(reply):
    return None if reply is None else reply.get("status")


def entries_of(reply):
    return None if reply is None else reply.get("entries")
