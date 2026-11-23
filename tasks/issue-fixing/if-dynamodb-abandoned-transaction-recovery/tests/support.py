"""Harness primitives for the verifier: own DynamoDB handle, seam hub,
subprocess driver. Independent of anything the solver ships."""

import json
import os
from pathlib import Path
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import uuid

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from cdk_seed.cfn_deploy import deploy

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

RPC_DEADLINE = 120.0        # per command, generous for 101-participant passes
BARRIER_DEADLINE = 60.0     # waiting for an announced pause
RECOVERY_DEADLINE = 15.0    # bounded recovery window after the last release
POLL_INTERVAL = 0.25        # quiescence polling cadence

ROLLBACK_AFTER_MS = 2000
AGE_MARGIN_MS = 250
RETENTION_MS = 300000
SHORT_RETENTION_MS = 1500
SWEEP_INTERVAL_MS = 100
PARTICIPANTS = 101

VALUE_BASELINE = 0
VALUE_A = 11
VALUE_B = 22
VALUE_SUCCESSOR = 33

_SER = TypeSerializer()
_DES = TypeDeserializer()


def _enc_item(item):
    return {name: _SER.serialize(value) for name, value in item.items()}


def _dec_item(attr_map):
    if not attr_map:
        return None
    out = {}
    for name, value in attr_map.items():
        out[name] = _DES.deserialize(value)
    return out


def _plain(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return value


class Dynamo(object):
    """The verifier's own client. It never imports solver code."""

    def __init__(self):
        self.client = boto3.client(
            "dynamodb",
            endpoint_url=ENDPOINT,
            region_name=REGION,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    def create_tables(self, tag):
        suffix = "%s%s" % (tag, uuid.uuid4().hex[:8])
        names = {
            "tx": "tx-" + suffix,
            "items": "items-" + suffix,
            "images": "images-" + suffix,
        }
        specs = [("tx", "txid"), ("items", "item_id"), ("images", "img")]
        cfn = boto3.client(
            "cloudformation",
            endpoint_url=self.client.meta.endpoint_url,
            region_name=self.client.meta.region_name,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )
        deploy(cfn, "recovery-" + suffix,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               TxTableName=names["tx"], ItemTableName=names["items"],
               ImageTableName=names["images"])
        for role, _ in specs:
            while True:
                resp = self.client.describe_table(TableName=names[role])
                if resp["Table"]["TableStatus"] == "ACTIVE":
                    break
                time.sleep(0.1)
        return names

    def drop_tables(self, names):
        for role in ("tx", "items", "images"):
            try:
                self.client.delete_table(TableName=names[role])
            except Exception:
                pass

    def seed(self, table, item_ids, value):
        for item_id in item_ids:
            self.client.put_item(TableName=table, Item=_enc_item(
                {"item_id": item_id, "val": value}))

    def get(self, table, key):
        resp = self.client.get_item(
            TableName=table, Key=_enc_item(key), ConsistentRead=True)
        return _dec_item(resp.get("Item"))

    def scan(self, table):
        rows, start = [], None
        while True:
            kwargs = {"TableName": table}
            if start:
                kwargs["ExclusiveStartKey"] = start
            resp = self.client.scan(**kwargs)
            for raw in resp.get("Items", []):
                rows.append(_dec_item(raw) or {})
            start = resp.get("LastEvaluatedKey")
            if not start:
                return rows

    def tx(self, names, txid):
        return self.get(names["tx"], {"txid": txid})

    def item_values(self, names):
        return {row["item_id"]: _plain(row.get("val"))
                for row in self.scan(names["items"])}

    def locked_items(self, names):
        return [row["item_id"] for row in self.scan(names["items"])
                if "owner" in row or "applied" in row or "locked_at_ms" in row]

    def image_count(self, names):
        return len(self.scan(names["images"]))

    def wait_age(self, names, txids, threshold_ms):
        deadline = time.time() + 60.0
        while time.time() < deadline:
            ages = []
            for txid in txids:
                record = self.tx(names, txid)
                if record is None:
                    return False
                ages.append(int(time.time() * 1000) - int(record["last_update_ms"]))
            if min(ages) > threshold_ms:
                return True
            time.sleep(0.1)
        return False


class SeamHub(object):
    """Listens for pause announcements. Announcements that match no hold
    rule are released immediately; matching ones stay blocked until the
    driver releases or kills the process."""

    def __init__(self, path):
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(32)
        self.listener.setblocking(False)
        self._lock = threading.Lock()
        self._rules = []
        self._holds = {}
        self._log = []
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop:
            try:
                readable, _, _ = select.select([self.listener], [], [], 0.02)
            except OSError:
                return
            if not readable:
                continue
            try:
                conn, _ = self.listener.accept()
            except (OSError, BlockingIOError):
                continue
            conn.settimeout(5.0)
            try:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    conn.close()
                    continue
                facts = json.loads(data.decode())
            except Exception:
                conn.close()
                continue
            with self._lock:
                self._log.append(dict(facts))
                holds = [rule for rule in self._rules if self._safe(rule, facts)]
            if holds:
                with self._lock:
                    self._holds[facts["pid"], facts["seam"]] = (conn, facts)
            else:
                try:
                    conn.sendall(b"1")
                except OSError:
                    pass
                conn.close()

    @staticmethod
    def _safe(rule, facts):
        try:
            return bool(rule(facts))
        except Exception:
            return False

    def set_rules(self, rules):
        with self._lock:
            self._rules = list(rules)

    def expect(self, predicate, timeout=BARRIER_DEADLINE, what="announcement"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for (pid, seam), (conn, facts) in self._holds.items():
                    if self._safe(predicate, facts):
                        return facts
            time.sleep(0.01)
        raise TimeoutError("never saw %s within %.0fs" % (what, timeout))

    def release(self, facts):
        with self._lock:
            entry = self._holds.pop((facts["pid"], facts["seam"]), None)
        if entry is None:
            return
        conn, _ = entry
        try:
            conn.sendall(b"1")
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def announcements(self):
        with self._lock:
            return list(self._log)

    def stop(self):
        self._stop = True
        try:
            self.listener.close()
        except OSError:
            pass
        with self._lock:
            holds = list(self._holds.values())
            self._holds = {}
        for conn, _facts in holds:
            try:
                conn.close()
            except OSError:
                pass


class Proc(object):
    """A transaction-program process with a response queue on stdout."""

    def __init__(self, name, role, tables, socket_path, extra_args=None):
        self.name = name
        self.workdir = "/tmp/proc-%s-%s" % (name, uuid.uuid4().hex[:6])
        os.makedirs(self.workdir, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": "/app",
            "PYTHONUNBUFFERED": "1",
            "SEAM_SOCKET": socket_path,
        })
        argv = [
            sys.executable, "-u", "/app/txnmgr.py", role,
            "--tx-table", tables["tx"],
            "--item-table", tables["items"],
            "--image-table", tables["images"],
            "--rollback-after-ms", str(ROLLBACK_AFTER_MS),
            "--retention-ms", str(RETENTION_MS),
            "--sweep-interval-ms", str(SWEEP_INTERVAL_MS),
        ]
        argv.extend(extra_args or [])
        self.pipe = subprocess.Popen(
            argv, cwd=self.workdir, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.responses = queue.Queue()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
        for line in self.pipe.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            message["_proc"] = self.name
            self.responses.put(message)
        self.responses.put({"_proc": self.name, "_eof": True})

    @property
    def pid(self):
        return self.pipe.pid

    def send(self, **command):
        self.pipe.stdin.write(json.dumps(command) + "\n")
        self.pipe.stdin.flush()

    def response(self, command_name, timeout=RPC_DEADLINE):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = self.responses.get(timeout=max(0.01, deadline - time.time()))
            except queue.Empty:
                break
            if message.get("cmd") == command_name:
                return message
        raise TimeoutError("%s: no response to %r within %.0fs"
                           % (self.name, command_name, timeout))

    def alive(self):
        return self.pipe.poll() is None

    def kill(self):
        try:
            self.pipe.kill()
            self.pipe.wait(timeout=10)
        except Exception:
            pass

    def close(self):
        try:
            self.pipe.stdin.close()
            self.pipe.wait(timeout=10)
        except Exception:
            self.kill()


def poll_until(predicate, timeout, interval=POLL_INTERVAL):
    deadline = time.time() + timeout
    detail = None
    while time.time() < deadline:
        detail = predicate()
        if detail is True:
            return True, None
        time.sleep(interval)
    return False, detail
