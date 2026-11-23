"""Shared fixtures for driving /app/program against Floci S3/SQS and an OpenSearch domain.

Expected values in the test modules come from native observations of the reference
(see the curation notes); this module only provisions inputs and reads back state.
"""

import contextlib
import gzip
import json
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import zstandard
from harness import give_to_candidate, start_command

REGION = "us-east-1"
ENDPOINT = "http://engines:4566"


def http(method, url, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def create_domain():
    client = boto3.client("opensearch")
    name = "bei" + uuid.uuid4().hex[:10]
    client.create_domain(DomainName=name, EngineVersion="OpenSearch_2.11")
    deadline = time.time() + 540
    while True:
        status = client.describe_domain(DomainName=name)["DomainStatus"]
        if status.get("Endpoint") and not status.get("Processing", False):
            break
        if time.time() > deadline:
            raise RuntimeError(f"OpenSearch domain not ready: {status}")
        time.sleep(2)
    endpoint = status["Endpoint"]
    url = (endpoint if "://" in endpoint else "http://" + endpoint).rstrip("/")
    while True:
        try:
            if http("GET", url + "/", timeout=5)[0] == 200:
                break
        except OSError:
            pass
        if time.time() > deadline:
            raise RuntimeError("OpenSearch endpoint not answering")
        time.sleep(2)
    return name, url


def delete_domain(name):
    with contextlib.suppress(Exception):
        boto3.client("opensearch").delete_domain(DomainName=name)


def gz(data):
    return gzip.compress(data, mtime=0)


def zstd_frames(*parts):
    compressor = zstandard.ZstdCompressor()
    return b"".join(compressor.compress(part) for part in parts)


def record(bucket, key, name="ObjectCreated:Put", region=REGION, version="2.1"):
    """One record of the standard S3 event notification structure; key already URL-encoded."""
    return {
        "eventVersion": version,
        "eventSource": "aws:s3",
        "awsRegion": region,
        "eventTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "eventName": name,
        "userIdentity": {"principalId": "EXAMPLE"},
        "requestParameters": {"sourceIPAddress": "127.0.0.1"},
        "responseElements": {"x-amz-request-id": "EXAMPLE123456789"},
        "s3": {
            "s3SchemaVersion": "1.0",
            "configurationId": "notify",
            "bucket": {"name": bucket, "ownerIdentity": {"principalId": "EXAMPLE"}, "arn": f"arn:aws:s3:::{bucket}"},
            "object": {"key": key, "size": 1, "eTag": "0123456789abcdef0123456789abcdef", "sequencer": "0A1B2C3D4E5F678901"},
        },
    }


def s3_test_event(bucket):
    return {
        "Service": "Amazon S3",
        "Event": "s3:TestEvent",
        "Time": "2014-10-13T15:57:02.089Z",
        "Bucket": bucket,
        "RequestId": "5582815E1AEA5ADF",
        "HostId": "8cLeGAmw098X5cv4Zkwcmo8vvZa3eH3eKxsPzbB9wrR+YstdA6Knx4Ip8EXAMPLE",
    }


def sns_envelope(message, timestamp=True):
    envelope = {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": "arn:aws:sns:us-east-1:000000000000:bucket-events",
        "Subject": "Amazon S3 Notification",
        "Message": json.dumps(message),
        "SignatureVersion": "1",
    }
    if timestamp:
        envelope["Timestamp"] = "2026-10-27T08:00:00.000Z"
    return envelope


def parse_instant(value):
    """RFC 3339 timestamp -> aware datetime (any fraction precision, Z or offset)."""
    assert isinstance(value, str), value
    match = re.fullmatch(r"(\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d)(\.\d+)?([Zz]|[+-]\d\d:\d\d)", value)
    assert match, f"not an RFC 3339 timestamp: {value!r}"
    base = datetime.strptime(match.group(1).upper(), "%Y-%m-%dT%H:%M:%S")
    fraction = timedelta(microseconds=int((match.group(2) or ".0")[1:7].ljust(6, "0")))
    zone = match.group(3)
    offset = timedelta(0) if zone in "Zz" else (1 if zone[0] == "+" else -1) * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    return (base + fraction - offset).replace(tzinfo=UTC)


def same(observed, expected):
    """Structural equality with value equivalence for numbers; booleans stay distinct."""
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return type(observed) is type(expected) and observed == expected
    if isinstance(expected, (int, float)):
        return type(observed) in (int, float) and observed == expected
    if isinstance(expected, dict):
        return isinstance(observed, dict) and observed.keys() == expected.keys() and all(same(observed[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(observed, list) and len(observed) == len(expected) and all(same(o, e) for o, e in zip(observed, expected, strict=True))
    raise TypeError(expected)


def assert_same_multiset(observed, expected):
    """Unordered comparison of complete documents, keeping multiplicity."""
    remaining = list(observed)
    for item in expected:
        for index, candidate in enumerate(remaining):
            if same(candidate, item):
                del remaining[index]
                break
        else:
            raise AssertionError(f"missing {item!r}\nobserved: {json.dumps(observed, indent=1, sort_keys=True)}")
    assert not remaining, f"unexpected extra documents: {remaining!r}"


class Pipeline:
    """One bucket, one queue and one index prefix, plus the program run against them."""

    def __init__(self, opensearch, root):
        self.os = opensearch
        self.root = root
        self.tag = uuid.uuid4().hex[:10]
        self.bucket = f"bei-{self.tag}"
        self.prefix = f"bei-{self.tag}"
        self.s3 = boto3.client("s3")
        self.sqs = boto3.client("sqs")
        self.s3.create_bucket(Bucket=self.bucket)
        self.queue = self.sqs.create_queue(QueueName=f"bei-{self.tag}")["QueueUrl"]
        self.process = None
        self.log = None
        self.cleanups = []
        self.configs = 0

    # ---- inputs
    def put(self, key, body, **options):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **options)
        return self.s3.head_object(Bucket=self.bucket, Key=key)

    def send(self, body):
        text = body if isinstance(body, str) else json.dumps(body)
        return self.sqs.send_message(QueueUrl=self.queue, MessageBody=text)["MessageId"]

    def notify(self, *keys, tag=None, **options):
        """Announce objects (keys given already URL-encoded) in one notification message."""
        body = {"Records": [record(self.bucket, key, **options) for key in keys]}
        if tag is not None:
            body["probeTag"] = tag
        return self.send(body)

    def config(self, *, codec="bytes", index=None, source="", sqs="", sink="", visibility=30, endpoint=None, acks=True, bulk=True, suffix=".toml", text=None):
        self.configs += 1
        data = self.root / f"state-{self.configs}"
        data.mkdir()
        give_to_candidate(data)
        path = self.root / f"pipeline-{self.configs}{suffix}"
        if text is None:
            mode = f'mode = "bulk"\nbulk.index = "{index or self.prefix + "-%Y.%m.%d"}"\n' if bulk else ""
            text = f"""data_dir = "{data}"

[sources.bucket]
type = "aws_s3"
region = "{REGION}"
endpoint = "{ENDPOINT}"
compression = "auto"
decoding.codec = "{codec}"
{source}

[sources.bucket.sqs]
queue_url = "{self.queue}"
poll_secs = 1
visibility_timeout_secs = {visibility}
{sqs}

[sinks.search]
type = "elasticsearch"
inputs = ["bucket"]
endpoints = ["{endpoint or self.os}"]
api_version = "v7"
batch.timeout_secs = 1
acknowledgements.enabled = {"true" if acks else "false"}
{mode}{sink}
"""
        path.write_text(text.replace("@DATA@", str(data)))
        path.chmod(0o644)
        return path

    # ---- program
    def start(self, config, flag="--config"):
        assert self.process is None
        self.log = open(self.root / f"program-{self.configs}.log", "wb")  # noqa: SIM115 -- closed in stop()
        self.process = start_command(["/app/program", flag, str(config)], cwd=str(self.root), stdout=self.log, stderr=subprocess.STDOUT)
        return self.process

    def stop(self, sig=signal.SIGTERM):
        """Signal the program and return its exit status."""
        process, self.process = self.process, None
        if process is None:
            return None
        if process.poll() is None:
            process.send_signal(sig)
        try:
            status = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            status = None
        self.log.close()
        return status

    def alive(self):
        return self.process is not None and self.process.poll() is None

    # ---- observation
    def hits(self, pattern=None):
        pattern = pattern or self.prefix + "*"
        http("POST", f"{self.os}/{pattern}/_refresh")
        scroll_id = None
        try:
            code, result = http("POST", f"{self.os}/{pattern}/_search?scroll=1m",
                                {"size": 5000, "sort": ["_doc"], "query": {"match_all": {}}, "version": True})
            if code == 404:
                return []
            found = []
            while True:
                scroll_id = result.get("_scroll_id", scroll_id)
                assert code == 200, result
                page = result["hits"]["hits"]
                if not page:
                    return found
                found.extend(page)
                assert scroll_id, "search did not provide a scroll ID"
                code, result = http("POST", f"{self.os}/_search/scroll", {"scroll": "1m", "scroll_id": scroll_id})
        finally:
            if scroll_id:
                with contextlib.suppress(Exception):
                    http("DELETE", f"{self.os}/_search/scroll", {"scroll_id": [scroll_id]})

    def count(self, pattern=None):
        return len(self.hits(pattern))

    def queue_counts(self):
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=self.queue, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
        )["Attributes"]
        return int(attributes["ApproximateNumberOfMessages"]), int(attributes["ApproximateNumberOfMessagesNotVisible"])

    def queued(self):
        return sum(self.queue_counts())

    def wait_for(self, predicate, timeout=40, interval=0.5, message="condition"):
        deadline = time.time() + timeout
        while True:
            value = predicate()
            if value:
                return value
            assert self.process is None or self.process.poll() is None, f"program exited early with {self.process.returncode}"
            if time.time() > deadline:
                raise AssertionError(f"timed out waiting for {message}")
            time.sleep(interval)

    def wait_settled(self, docs, queued=0, pattern=None, timeout=40, settle=2.0):
        """Wait until the index holds `docs` documents and `queued` messages remain, then re-check."""
        self.wait_for(lambda: self.queued() == queued and self.count(pattern) >= docs, timeout, message=f"{docs} docs and {queued} queued")
        time.sleep(settle)
        assert self.queued() == queued
        found = self.hits(pattern)
        assert len(found) == docs, [h["_source"] for h in found]
        return found

    def remaining(self, wait=12):
        """After stopping the program, receive every remaining message (tags and receive counts)."""
        assert self.process is None
        found = {}
        deadline = time.time() + wait
        while time.time() < deadline:
            response = self.sqs.receive_message(
                QueueUrl=self.queue, MaxNumberOfMessages=10, WaitTimeSeconds=1, VisibilityTimeout=120, AttributeNames=["ApproximateReceiveCount"]
            )
            for message in response.get("Messages", []):
                try:
                    tag = json.loads(message["Body"]).get("probeTag", message["Body"])
                except (ValueError, AttributeError):
                    tag = message["Body"]
                found[message["MessageId"]] = (tag, int(message["Attributes"]["ApproximateReceiveCount"]))
            if self.queue_counts() == (0, 0):
                break
        return found

    def close(self):
        if self.process is not None:
            self.stop()
        for cleanup in reversed(self.cleanups):
            with contextlib.suppress(Exception):
                cleanup()
        with contextlib.suppress(Exception):
            http("DELETE", f"{self.os}/{self.prefix}*")
        with contextlib.suppress(Exception):
            self.sqs.delete_queue(QueueUrl=self.queue)
        with contextlib.suppress(Exception):
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
                for item in page.get("Contents", []):
                    self.s3.delete_object(Bucket=self.bucket, Key=item["Key"])
            self.s3.delete_bucket(Bucket=self.bucket)


def by_id(found):
    return {hit["_id"]: hit for hit in found}


def strip_time(source):
    return {key: value for key, value in source.items() if key != "timestamp"}


class FaultProxy:
    """HTTP intermediary in front of OpenSearch that can fail or partially fail bulk requests.

    `plan(n, items)` is called for the n-th bulk request (1-based) with its parsed
    (action, metadata, source) items and returns None (forward unchanged), an HTTP status
    for a whole-request failure, or {_id: status} for per-item failures injected into an
    otherwise forwarded request. Every bulk request is recorded.
    """

    def __init__(self, target, plan):
        self.target = target
        self.plan = plan
        self.requests = []
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                status, payload = proxy.handle(self.command, self.path, dict(self.headers), body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=UTF-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def forward(self, method, path, headers, body):
        keep = {k: v for k, v in headers.items() if k.lower() in ("content-type", "content-encoding", "accept")}
        request = urllib.request.Request(self.target + path, data=body, method=method, headers=keep)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def handle(self, method, path, headers, body):
        if not path.split("?")[0].endswith("/_bulk"):
            return self.forward(method, path, headers, body)
        data = body or b""
        if (headers.get("Content-Encoding") or headers.get("content-encoding") or "").lower() == "gzip":
            data = gzip.decompress(data)
        lines = [line for line in data.decode().split("\n") if line.strip()]
        items, index = [], 0
        while index < len(lines):
            action = json.loads(lines[index])
            (operation, metadata), = action.items()
            source = None if operation == "delete" else json.loads(lines[index + 1])
            items.append((operation, metadata, source))
            index += 1 if source is None else 2
        with self.lock:
            self.requests.append(items)
            number = len(self.requests)
        decision = self.plan(number, items)
        if decision is None:
            return self.forward(method, path, headers, body)
        if isinstance(decision, int):
            return decision, json.dumps({"error": {"type": "injected_failure", "reason": "injected"}, "status": decision}, separators=(",", ":")).encode()
        kept = [item for item in items if item[1].get("_id") not in decision]
        responses = iter([])
        if kept:
            payload = "".join(json.dumps({op: meta}) + "\n" + json.dumps(src) + "\n" for op, meta, src in kept).encode()
            status, answer = self.forward("POST", path, {"Content-Type": "application/x-ndjson"}, payload)
            assert status == 200, answer
            responses = iter(json.loads(answer)["items"])
        out = []
        for operation, metadata, _source in items:
            if metadata.get("_id") in decision:
                code = decision[metadata["_id"]]
                kind = "es_rejected_execution_exception" if code == 429 else "mapper_parsing_exception"
                out.append({operation: {"_index": metadata.get("_index"), "_id": metadata["_id"], "status": code, "error": {"type": kind, "reason": "injected"}}})
            else:
                out.append(next(responses))
        return 200, json.dumps({"took": 1, "errors": True, "items": out}, separators=(",", ":")).encode()

    def ids(self):
        with self.lock:
            return [[meta.get("_id") for _op, meta, _src in items] for items in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
