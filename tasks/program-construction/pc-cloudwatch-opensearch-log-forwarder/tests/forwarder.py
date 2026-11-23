"""Shared fixtures for driving the forwarder: configs, processes, CloudWatch events, OpenSearch reads."""

import datetime
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

import boto3

from harness import give_to_candidate, run_program, start_command, write_fixture

SCOPE_NAME = "github.com/open-telemetry/opentelemetry-collector-contrib/receiver/awscloudwatchreceiver"
SCOPE_VERSION = "0.163.0"
REGION = "us-east-1"


def now_ms():
    return int(time.time() * 1000)


def rfc3339_seconds(ms):
    return datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- YAML configs

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)  # a JSON string is a valid YAML double-quoted scalar


def to_yaml(mapping, indent=0):
    lines = []
    pad = " " * indent
    for key, value in mapping.items():
        key = key if _PLAIN_KEY.match(key) else json.dumps(key)
        if isinstance(value, dict) and value:
            lines.append(f"{pad}{key}:")
            lines.append(to_yaml(value, indent + 2))
        elif isinstance(value, dict):
            lines.append(f"{pad}{key}: {{}}")
        elif isinstance(value, list):
            lines.append(f"{pad}{key}: [{', '.join(_scalar(item) for item in value)}]")
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")
    return "\n".join(lines)


def config(endpoint, *, named=None, autodiscover=None, logs=None, exporter=None, storage=None, batch=True):
    """A complete forwarder configuration; `logs` and `exporter` add or override keys."""
    groups = {"named": named} if named is not None else {"autodiscover": autodiscover}
    receiver = {"region": REGION}
    if storage is not None:
        receiver["storage"] = "file_storage"
    receiver["logs"] = {"poll_interval": "1s", "groups": groups, **(logs or {})}
    document = {}
    if storage is not None:
        document["extensions"] = {"file_storage": {"directory": str(storage)}}
    document["receivers"] = {"aws_cloudwatch": receiver}
    if batch:
        document["processors"] = {"batch": {"timeout": "200ms"}}
    document["exporters"] = {"opensearch": {"http": {"endpoint": endpoint}, **(exporter or {})}}
    pipeline = {"receivers": ["aws_cloudwatch"], "exporters": ["opensearch"]}
    if batch:
        pipeline = {"receivers": ["aws_cloudwatch"], "processors": ["batch"], "exporters": ["opensearch"]}
    document["service"] = {
        "extensions": ["file_storage"] if storage is not None else [],
        "telemetry": {"metrics": {"level": "none"}},
        "pipelines": {"logs": pipeline},
    }
    return document


# ------------------------------------------------------------------- process


class Collector:
    """A running forwarder; always stopped and reaped on exit."""

    def __init__(self, config_path, cwd, env=None, extra_args=()):
        sources = ["--config", f"file:{config_path}"] if config_path is not None else []
        self.args = ["/app/program", *sources, *extra_args]
        self.cwd = cwd
        self.env = env
        self.process = None
        self.returncode = None
        self._logdir = tempfile.mkdtemp(prefix="forwarder-log-")  # root-private
        self.logfile = Path(self._logdir) / "program.log"

    def __enter__(self):
        with self.logfile.open("wb") as log:
            self.process = start_command(self.args, cwd=self.cwd, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        return self

    def stop(self, sig=signal.SIGTERM, timeout=20):
        if self.process is None or self.returncode is not None:
            return self.returncode
        if self.process.poll() is None:
            self.process.send_signal(sig)
        try:
            self.returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise AssertionError("the program did not stop within %ss of signal %s" % (timeout, sig)) from None
        return self.returncode

    def running(self):
        return self.process.poll() is None

    def log(self):
        return self.logfile.read_text(errors="replace")[-4000:]

    def __exit__(self, *exc):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
            if self.process is not None:
                self.process.wait()
        finally:
            self.logfile.unlink(missing_ok=True)
            os.rmdir(self._logdir)


# ---------------------------------------------------------------- OpenSearch


class Search:
    def __init__(self, endpoint):
        self.endpoint = endpoint.rstrip("/")

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.endpoint + path, method=method, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def hits(self, target):
        """All (index, _source) pairs in an index, data stream or pattern; [] when absent."""
        deadline = time.monotonic() + 15
        while True:
            self.request("POST", f"/{target}/_refresh?expand_wildcards=all")
            status, body = self.request("POST", f"/{target}/_search?expand_wildcards=all", {"size": 1000, "query": {"match_all": {}}})
            if status == 404:
                return []
            if status == 200 and not body["_shards"].get("failed"):
                return [(hit["_index"], hit["_source"]) for hit in body["hits"]["hits"]]
            # A freshly created index may not have its shards allocated yet.
            assert status == 503 or status == 200, body
            assert time.monotonic() < deadline, body
            time.sleep(0.5)

    def count(self, target):
        return len(self.hits(target))

    def wait_count(self, target, expected, timeout):
        deadline = time.monotonic() + timeout
        while True:
            found = self.count(target)
            if found >= expected or time.monotonic() > deadline:
                return found
            time.sleep(0.25)

    def indices(self, pattern):
        """{index name: document count} for concrete indices matching pattern."""
        self.request("POST", f"/{pattern}/_refresh?expand_wildcards=all")
        status, body = self.request("GET", f"/_cat/indices/{pattern}?format=json&expand_wildcards=all")
        if status == 404:
            return {}
        assert status == 200, body
        return {entry["index"]: int(entry["docs.count"]) for entry in body}


# ---------------------------------------------------------- CloudWatch Logs


class Case:
    """Per-test namespace: unique log groups, indices, storage and configs; all removed afterwards."""

    def __init__(self, root, search):
        self.root = root
        self.search = search
        self.id = uuid.uuid4().hex[:10]
        self.namespace = "ns" + self.id
        self.logs = boto3.client("logs")
        self.groups = []
        self.cleanups = []
        self.started_ms = now_ms()
        self._account = None

    def name(self, suffix):
        return f"/fwd/{self.id}/{suffix}"

    def index(self, dataset):
        return f"ss4o_logs-{dataset}-{self.namespace}"

    def group(self, suffix, *streams):
        group = self.name(suffix)
        self.logs.create_log_group(logGroupName=group)
        self.groups.append(group)
        for stream in streams:
            self.logs.create_log_stream(logGroupName=group, logStreamName=stream)
        return group

    def delete_group(self, group):
        self.logs.delete_log_group(logGroupName=group)
        self.groups.remove(group)

    def put(self, group, stream, *events):
        """events: (timestamp_ms, message) pairs, in ascending time order."""
        self.logs.put_log_events(
            logGroupName=group, logStreamName=stream, logEvents=[{"timestamp": ts, "message": message} for ts, message in events]
        )

    def events(self, group):
        """CloudWatch's own record of the group's events, keyed by message."""
        found = {}
        kwargs = {"logGroupName": group, "startTime": 0}
        while True:
            page = self.logs.filter_log_events(**kwargs)
            for event in page["events"]:
                assert event["message"] not in found, "fixture messages must be unique"
                found[event["message"]] = {**event, "logGroupName": group}
            if not page.get("nextToken"):
                return found
            kwargs["nextToken"] = page["nextToken"]

    @property
    def account(self):
        if self._account is None:
            self._account = boto3.client("sts").get_caller_identity()["Account"]
        return self._account

    def storage(self, name="state"):
        directory = self.root / name
        directory.mkdir(mode=0o755)
        give_to_candidate(directory)
        return directory

    def write_config(self, name, document):
        path = self.root / name
        write_fixture(path, (to_yaml(document) if isinstance(document, dict) else document) + "\n", mode=0o644)
        return path

    def collector(self, config_path, **kwargs):
        return Collector(config_path, self.root, **kwargs)

    def run(self, config_path, target, count, *, timeout=20, settle=3, env=None):
        """Run until `count` documents exist in `target` (or timeout), keep running `settle` seconds, then SIGTERM."""
        with self.collector(config_path, env=env) as program:
            self.search.wait_count(target, count, timeout)
            time.sleep(settle)
            assert program.running(), "the program exited while polling:\n" + program.log()
            status = program.stop()
            assert status == 0, f"exit status {status} after SIGTERM:\n" + program.log()

    def validate(self, config_path, env=None):
        return run_program(["validate", "--config", f"file:{config_path}"], cwd=self.root, env=env)

    def expected(self, event, dataset, namespace=None, index=None):
        """The document the reference indexes for a CloudWatch event (observedTimestamp checked separately)."""
        return (
            index or f"ss4o_logs-{dataset}-{namespace or self.namespace}",
            {
                "@timestamp": event["timestamp"],
                "attributes": {
                    "data_stream": {"dataset": dataset, "namespace": namespace or self.namespace, "type": "record"},
                    "id": event["eventId"],
                },
                "body": event["message"],
                "instrumentationScope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                "resource": {
                    "aws.region": REGION,
                    "cloud.account.id": self.account,
                    "cloudwatch.log.group.name": event["logGroupName"],
                    "cloudwatch.log.stream": event["logStreamName"],
                },
                "severity": {},
            },
        )

    def cleanup(self):
        for action in reversed(self.cleanups):
            try:
                action()
            except Exception:  # noqa: BLE001 -- teardown is best effort
                pass
        for group in list(self.groups):
            try:
                self.logs.delete_log_group(logGroupName=group)
            except Exception:  # noqa: BLE001
                pass
        self.search.request("DELETE", f"/_data_stream/*{self.id}*")
        self.search.request("DELETE", f"/*{self.id}*?expand_wildcards=all")


# ------------------------------------------------------------- comparisons

_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?([Zz]|[+-]\d\d:\d\d)")


def instant_ms(text):
    """Milliseconds since the epoch for an RFC 3339 timestamp string, or None if it is not one."""
    if not isinstance(text, str):
        return None
    match = _RFC3339.fullmatch(text)
    if not match:
        return None
    base, fraction, zone = match.groups()
    moment = datetime.datetime.fromisoformat(base.upper() + ("+00:00" if zone in "Zz" else zone))
    nanos = int((fraction or "").ljust(9, "0"))
    if nanos % 1_000_000:
        return None  # CloudWatch timestamps have millisecond precision
    return int(moment.timestamp()) * 1000 + nanos // 1_000_000


def _canonical(index, source):
    """Compare whole documents: every key and JSON type matters; @timestamp by instant; observedTimestamp aside."""
    document = dict(source)
    document.pop("observedTimestamp", None)
    if "@timestamp" in document:
        parsed = instant_ms(document["@timestamp"])
        document["@timestamp"] = ["instant", parsed] if parsed is not None else ["raw", document["@timestamp"]]
    return json.dumps([index, document], sort_keys=True, ensure_ascii=False)


def _expected_canonical(index, document):
    document = dict(document)
    document["@timestamp"] = ["instant", document["@timestamp"]]
    return json.dumps([index, document], sort_keys=True, ensure_ascii=False)


def assert_documents(hits, expected, observed_after_ms):
    """hits: [(index, _source)]; expected: [(index, document)]. Multiset equality, any order."""
    actual = Counter(_canonical(index, source) for index, source in hits)
    wanted = Counter(_expected_canonical(index, document) for index, document in expected)
    missing = list((wanted - actual).elements())
    extra = list((actual - wanted).elements())
    assert not missing and not extra, "document mismatch\nmissing:\n  " + "\n  ".join(missing) + "\nunexpected:\n  " + "\n  ".join(extra)
    upper = now_ms() + 2000
    for _, source in hits:
        observed = instant_ms_any(source.get("observedTimestamp"))
        assert observed is not None, f"observedTimestamp is not an RFC 3339 timestamp: {source.get('observedTimestamp')!r}"
        assert observed_after_ms - 2000 <= observed <= upper, f"observedTimestamp {source['observedTimestamp']} is not the export time"


def instant_ms_any(text):
    """Like instant_ms but accepting sub-millisecond precision (wall-clock timestamps)."""
    if not isinstance(text, str):
        return None
    match = _RFC3339.fullmatch(text)
    if not match:
        return None
    base, fraction, zone = match.groups()
    moment = datetime.datetime.fromisoformat(base.upper() + ("+00:00" if zone in "Zz" else zone))
    return int(moment.timestamp()) * 1000 + int((fraction or "").ljust(9, "0")) // 1_000_000


def bodies(hits):
    return sorted(source.get("body") for _, source in hits)
