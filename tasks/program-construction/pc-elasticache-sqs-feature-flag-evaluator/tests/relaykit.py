"""Shared helpers: run the candidate service, talk HTTP to it, read SQS, compare JSON."""

import json
import tempfile
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import boto3
import redis

from harness import start_command, write_fixture

EMITTER = {"DataType": "String", "StringValue": "GO Feature Flag"}
# Fields the public observation rules leave ungraded on /v1/... responses.
V1_UNGRADED = ("timestamp", "cacheable")


def unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Store:
    """One key prefix inside the shared Valkey engine; flags are JSON strings."""

    def __init__(self, endpoint, prefix=None, db=0):
        self.endpoint = endpoint
        self.db = db
        self.prefix = unique("t") + ":" if prefix is None else prefix
        self.client = redis.Redis(host=endpoint[0], port=endpoint[1], db=db)

    def put(self, name, flag):
        self.client.set(self.prefix + name, flag if isinstance(flag, (str, bytes)) else json.dumps(flag))

    def put_all(self, flags):
        for name, flag in flags.items():
            self.put(name, flag)

    def delete(self, name):
        self.client.delete(self.prefix + name)

    def cleanup(self):
        for key in list(self.client.scan_iter(match=self.prefix + "*")):
            self.client.delete(key)


def config_text(port, store=None, queue_url=None, prefix=True, db=None, addr=None):
    lines = [f"server:\n  port: {port}\n", "hideBanner: true\n", "pollingInterval: 1000\n"]
    if store is not None or addr is not None:
        address = addr or f"{store.endpoint[0]}:{store.endpoint[1]}"
        lines.append(f"retrievers:\n  - kind: redis\n    redisOptions:\n      addr: \"{address}\"\n")
        if db is not None:
            lines.append(f"      db: {db}\n")
        if prefix and store is not None:
            lines.append(f"    redisPrefix: \"{store.prefix}\"\n")
    if queue_url is not None:
        lines.append(f"exporters:\n  - kind: sqs\n    queueUrl: \"{queue_url}\"\n")
    return "".join(lines)


class Relay:
    """A running candidate service started with `--config`."""

    def __init__(self, directory, config, port):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.config = directory / f"relay-{port}.yaml"
        write_fixture(self.config, config, mode=0o644)
        # The log lives in a verifier-private directory; the candidate only inherits the descriptor.
        self.log_path = Path(tempfile.mkdtemp(prefix="relay-log-")) / "relay.log"
        self.log = open(self.log_path, "wb")  # noqa: SIM115 -- closed in stop()
        self.proc = start_command(["/app/program", "--config", str(self.config)], cwd=directory,
                                  stdout=self.log, stderr=subprocess.STDOUT)

    def log_tail(self):
        try:
            return self.log_path.read_text(errors="replace")[-3000:]
        except OSError:
            return ""

    def wait_ready(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"service exited with {self.proc.returncode} before serving:\n{self.log_tail()}")
            try:
                if self.request("GET", "/health", timeout=2)[0] == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        raise AssertionError(f"service did not answer /health:\n{self.log_tail()}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()

    def request(self, method, path, body=None, raw=None, timeout=10):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, text = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, text = err.code, err.read()
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, text.decode(errors="replace")

    def ofrep(self, key, context):
        return self.request("POST", f"/ofrep/v1/evaluate/flags/{key}", {"context": context})

    def bulk(self, context):
        return self.request("POST", "/ofrep/v1/evaluate/flags", {"context": context})

    def feature(self, key, body):
        return self.request("POST", f"/v1/feature/{key}/eval", body)

    def allflags(self, body):
        return self.request("POST", "/v1/allflags", body)


def wait_for(probe, check, timeout=12, interval=0.25):
    """Poll `probe()` until `check(result)` holds; return the last result."""
    deadline = time.time() + timeout
    result = probe()
    while not check(result) and time.time() < deadline:
        time.sleep(interval)
        result = probe()
    return result


def drain(queue_url, expected, timeout=20, quiet=3):
    """Receive and delete messages until `expected` arrived and the queue stayed quiet."""
    sqs = boto3.client("sqs")
    messages, deadline, idle = [], time.time() + timeout, 0
    while time.time() < deadline:
        batch = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                    MessageAttributeNames=["All"]).get("Messages", [])
        for message in batch:
            messages.append(message)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        idle = 0 if batch else idle + 1
        if len(messages) >= expected and idle >= quiet:
            break
    return messages


# ---- comparison policy (see /docs/interface.md "Observation rules") -------------

def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def same(actual, expected):
    """Decoded-JSON equality keeping booleans, numbers and null distinct; numbers by value."""
    if _kind(actual) != _kind(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(same(actual[k], expected[k]) for k in expected)
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(same(a, e) for a, e in zip(actual, expected))
    return actual == expected


def _normal(body, ignore=()):
    """Apply the public equivalences: absent metadata == {}; errorDetails is free text, presence ungraded."""
    if not isinstance(body, dict):
        return body
    out = {k: v for k, v in body.items() if k not in ignore}
    if out.get("metadata") == {}:
        del out["metadata"]
    if "errorDetails" in out:
        assert isinstance(out["errorDetails"], str), f"errorDetails must be a string: {body!r}"
        del out["errorDetails"]
    return out


def assert_json(actual, expected, ignore=(), label=""):
    a, e = _normal(actual, ignore), _normal(expected, ignore)
    assert same(a, e), f"{label}\nexpected: {json.dumps(e, sort_keys=True)}\nactual:   {json.dumps(actual, sort_keys=True)}"


def assert_response(response, status, body, ignore=(), label=""):
    got_status, got_body = response
    assert got_status == status, f"{label} status {got_status} != {status}; body {got_body!r}"
    assert_json(got_body, body, ignore, label)


def assert_bulk(response, flags, label=""):
    """OFREP bulk: ordered list of per-flag results."""
    status, body = response
    assert status == 200, f"{label} bulk status {status}: {body!r}"
    assert isinstance(body, dict) and set(body) == {"flags"}, f"{label} bulk body {body!r}"
    got = body["flags"]
    assert isinstance(got, list), body
    assert [f.get("key") for f in got] == [f["key"] for f in flags], f"{label} keys {[f.get('key') for f in got]}"
    for actual, expected in zip(got, flags):
        assert_json(actual, expected, label=f"{label} {expected['key']}")


def assert_allflags(response, valid, flags, label=""):
    status, body = response
    assert status == 200, f"{label} status {status}: {body!r}"
    assert isinstance(body, dict), body
    assert type(body.get("valid")) is bool and body["valid"] is valid, f"{label} valid {body!r}"
    if flags is None:
        assert set(body) == {"valid"}, f"{label} {body!r}"
        return
    assert set(body) == {"valid", "flags"} and set(body["flags"]) == set(flags), f"{label} {body!r}"
    for name, expected in flags.items():
        assert_json(body["flags"][name], expected, ignore=V1_UNGRADED, label=f"{label} {name}")


def event_bodies(messages, start, end):
    """Decode events, check the emitter attribute and the creationDate window."""
    bodies = []
    for message in messages:
        assert message.get("MessageAttributes", {}).get("emitter") == EMITTER, message.get("MessageAttributes")
        body = json.loads(message["Body"])
        created = body.pop("creationDate", None)
        assert _kind(created) == "number" and start - 5 <= created <= end + 5, f"creationDate {created!r}"
        body.pop("metadata", None)
        bodies.append(body)
    return bodies


def feature_event(key, user, variation, value, default=False, version="", kind="user"):
    return {"kind": "feature", "contextKind": kind, "userKey": user, "key": key, "variation": variation,
            "value": value, "default": default, "version": version, "source": "SERVER"}


def assert_events(bodies, expected):
    """Unordered comparison keeping multiplicity."""
    remaining = list(expected)
    unexpected = []
    for body in bodies:
        for i, want in enumerate(remaining):
            if same(body, want):
                del remaining[i]
                break
        else:
            unexpected.append(body)
    assert not remaining and not unexpected, (
        f"missing events: {json.dumps(remaining, sort_keys=True)}\nunexpected events: {json.dumps(unexpected, sort_keys=True)}")
