"""Task-local helpers: cluster HTTP access, daemon lifecycle, output readers and comparisons."""

import datetime as dt
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3

from harness import give_to_candidate, start_command

STATE_PATTERN = "go-es-alerts-status-0.0.3-*"
STATE_ALIAS = "go-es-alerts-0.0.3"
BODY_DELIMITER = "\n" + "-" * 40 + "\n"


def tag():
    return uuid.uuid4().hex[:8]


class Cluster:
    """Plain HTTP access to the OpenSearch domain the daemon talks to."""

    def __init__(self, url):
        self.url = url.rstrip("/")

    def http(self, method, path, body=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, raw.decode(errors="replace")

    def raw(self, method, path, text):
        """Send a JSON body exactly as written (number spellings included)."""
        request = urllib.request.Request(self.url + path, data=text.encode(), method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(errors="replace")

    def create_index(self, name, properties, docs):
        """docs are dicts, or JSON text indexed verbatim."""
        status, body = self.http("PUT", f"/{name}", {"settings": {"number_of_replicas": 0}, "mappings": {"properties": properties}})
        assert status == 200, (status, body)
        for number, doc in enumerate(docs):
            status, body = self.http("PUT", f"/{name}/_doc/{number}", doc) if not isinstance(doc, str) else self.raw("PUT", f"/{name}/_doc/{number}", doc)
            assert status in (200, 201), (status, body)
        self.http("POST", f"/{name}/_refresh")

    def delete_index(self, name):
        self.http("DELETE", f"/{name}")

    def refresh_state(self):
        self.http("POST", f"/{STATE_PATTERN}/_refresh")

    def state_docs(self, rule_name):
        """Every state document whose rule_name is exactly this value, oldest write first."""
        self.refresh_state()
        status, body = self.http(
            "GET",
            f"/{STATE_PATTERN}/_search",
            {"size": 500, "query": {"term": {"rule_name": rule_name}}, "sort": [{"@timestamp": "asc"}, {"next_query": "asc"}]},
        )
        if status == 404:
            return []
        assert status == 200, (status, body)
        return [hit["_source"] for hit in body["hits"]["hits"]]

    def state_hits(self, rule_name):
        """Full search hits (with _index) for this rule's state documents."""
        self.refresh_state()
        status, body = self.http("GET", f"/{STATE_PATTERN}/_search", {"size": 500, "query": {"term": {"rule_name": rule_name}}})
        assert status == 200, (status, body)
        return body["hits"]["hits"]

    def seed_state(self, index, docs):
        """Pre-existing state documents written by the test environment (the index gets the state alias)."""
        assert index.startswith("go-es-alerts-status-0.0.3-")
        for doc in docs:
            status, body = self.http("POST", f"/{index}/_doc", doc)
            assert status == 201, (status, body)
        self.http("POST", f"/{index}/_refresh")


class Daemon:
    """Configuration, rules and lifecycle of one daemon instance in a candidate workspace."""

    def __init__(self, root: Path, cluster: Cluster, config=None):
        self.root = root
        self.cluster = cluster
        self.rules = root / "rules"
        self.rules.mkdir(mode=0o755, exist_ok=True)
        self.config = root / "config.json"
        self.write_config(config if config is not None else {"elasticsearch": {"server": {"url": cluster.url}}})
        self.process = None
        self.stderr = None
        self.log = ""

    def write_config(self, config):
        text = config if isinstance(config, str) else json.dumps(config)
        self.config.write_text(text)
        self.config.chmod(0o644)

    def add_rule(self, filename, rule):
        path = self.rules / filename
        path.write_text(rule if isinstance(rule, str) else json.dumps(rule, indent=2))
        path.chmod(0o644)
        return path

    def env(self, extra=None):
        return {
            "GO_ELASTICSEARCH_ALERTS_CONFIG_FILE": str(self.config),
            "GO_ELASTICSEARCH_ALERTS_RULES_DIR": str(self.rules),
            **(extra or {}),
        }

    def start(self, env=None):
        assert self.process is None
        self.stderr = tempfile.TemporaryFile()
        self.process = start_command(["/app/program"], cwd=self.root, env=self.env(env), stdout=self.stderr, stderr=self.stderr)
        self.started = now()
        return self

    def running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self, sig=signal.SIGTERM, timeout=15):
        """Signal the daemon and return its exit status (None if it had to be killed)."""
        if self.process is None:
            return None
        status = self.process.poll()
        if status is None:
            self.process.send_signal(sig)
            try:
                status = self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                status = None
        self.stderr.seek(0)
        self.log = self.stderr.read().decode(errors="replace")
        self.stderr.close()
        self.process = None
        return status

    def run_to_exit(self, env=None, timeout=15):
        """For configurations expected to be rejected: start and wait for the process to exit by itself."""
        self.start(env)
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        finally:
            self.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def output_dir(root: Path, name="out"):
    """A candidate-writable directory for file outputs."""
    path = root / name
    path.mkdir(mode=0o755)
    give_to_candidate(path)
    return path


def wait_until(predicate, timeout, interval=0.25):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def wait_state(cluster, rule_name, count=1, timeout=20):
    """Wait until the rule has at least `count` state documents; returns them all."""
    docs = wait_until(lambda: (lambda d: d if len(d) >= count else None)(cluster.state_docs(rule_name)), timeout, 0.5)
    return docs or cluster.state_docs(rule_name)


def read_lines(path: Path):
    """Decoded JSON lines of a file output (empty when it does not exist)."""
    if not os.path.lexists(path):
        return []
    with open(path, "rb") as handle:
        content = handle.read().decode()
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def wait_lines(path, count, timeout=15):
    return wait_until(lambda: (lambda lines: lines if len(lines) >= count else None)(read_lines(path)), timeout) or read_lines(path)


def cleaned(name):
    """Expected state rule_name for a rule name made of lowercase-safe ASCII words."""
    return name.lower().replace(" ", "-")


def now():
    return dt.datetime.now(dt.timezone.utc)


_RFC3339 = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$")


def instant(text):
    """Parse an RFC 3339 timestamp (any offset, any fraction length) to an aware datetime."""
    assert isinstance(text, str), text
    match = _RFC3339.match(text)
    assert match, f"not an RFC 3339 timestamp: {text!r}"
    base, fraction, zone = match.groups()
    fraction = (fraction or ".0")[1:7].ljust(6, "0")
    zone = "+00:00" if zone == "Z" else zone
    return dt.datetime.fromisoformat(f"{base}.{fraction}{zone}")


def rfc3339(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def same(observed, expected, where="$"):
    """JSON equality: numbers by value (int/float alike), booleans distinct from numbers, lists ordered."""
    if isinstance(expected, bool) or isinstance(observed, bool):
        assert type(observed) is bool and type(expected) is bool and observed == expected, f"{where}: {observed!r} != {expected!r}"
    elif isinstance(expected, (int, float)):
        assert type(observed) in (int, float) and observed == expected, f"{where}: {observed!r} != {expected!r}"
    elif isinstance(expected, dict):
        assert isinstance(observed, dict), f"{where}: {observed!r} is not an object"
        assert set(observed) == set(expected), f"{where}: keys {sorted(observed)} != {sorted(expected)}"
        for key in expected:
            same(observed[key], expected[key], f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(observed, list) and len(observed) == len(expected), f"{where}: {observed!r} != {expected!r}"
        for number, (left, right) in enumerate(zip(observed, expected)):
            same(left, right, f"{where}[{number}]")
    else:
        assert type(observed) is type(expected) and observed == expected, f"{where}: {observed!r} != {expected!r}"


def body_text(*objects):
    """The documented body_field rendering of an alert, given the exact per-object renderings."""
    return BODY_DELIMITER.join(objects)


def check_state_doc(doc, rule_name, hits=None):
    """A state document's complete shape; returns its parsed (@timestamp, next_query)."""
    expected_keys = {"@timestamp", "rule_name", "next_query", "hostname", "hits_count"} | ({"hits"} if hits else set())
    assert set(doc) == expected_keys, doc
    assert doc["rule_name"] == rule_name
    assert doc["hostname"] == socket.gethostname()
    same(doc["hits_count"], len(hits or []), "hits_count")
    if hits:
        same(doc["hits"], hits, "hits")
    return instant(doc["@timestamp"]), instant(doc["next_query"])


class Topic:
    """An SNS topic with a subscribed SQS queue for observing published alerts."""

    def __init__(self):
        self.sns = boto3.client("sns")
        self.sqs = boto3.client("sqs")
        name = "qad-" + tag()
        self.arn = self.sns.create_topic(Name=name)["TopicArn"]
        self.queue = None
        self.subscription = None
        try:
            self.queue = self.sqs.create_queue(QueueName=name)["QueueUrl"]
            queue_arn = self.sqs.get_queue_attributes(QueueUrl=self.queue, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            self.subscription = self.sns.subscribe(TopicArn=self.arn, Protocol="sqs", Endpoint=queue_arn, ReturnSubscriptionArn=True)["SubscriptionArn"]
        except Exception:
            self.close()
            raise
        self.received = []

    def poll(self, seconds=1):
        response = self.sqs.receive_message(QueueUrl=self.queue, MaxNumberOfMessages=10, WaitTimeSeconds=seconds)
        for message in response.get("Messages", []):
            self.received.append(json.loads(message["Body"])["Message"])
            self.sqs.delete_message(QueueUrl=self.queue, ReceiptHandle=message["ReceiptHandle"])
        return self.received

    def messages(self, count, timeout=15):
        """Collect until `count` messages arrived (or timeout), then linger briefly for extras."""
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            self.poll(1)
        self.poll(1)
        return list(self.received)

    def close(self):
        for action in (
            lambda: self.subscription and self.sns.unsubscribe(SubscriptionArn=self.subscription),
            lambda: self.queue and self.sqs.delete_queue(QueueUrl=self.queue),
            lambda: self.sns.delete_topic(TopicArn=self.arn),
        ):
            try:
                action()
            except Exception:
                pass


def run_once(daemon, cluster, rule_names, settle=1.5, timeout=25):
    """Start the daemon, wait for every rule's first state document, let outputs drain, stop it."""
    daemon.start()
    try:
        for name in rule_names:
            assert wait_state(cluster, name, 1, timeout), f"no state document for {name}"
        time.sleep(settle)
    finally:
        status = daemon.stop()
    assert status == 0, daemon.log[-3000:]
    return daemon.log


def file_rule(name, index, body, out, filename, **fields):
    """A rule dict with a single file output."""
    return {
        "name": name,
        "index": index,
        "schedule": fields.pop("schedule", "@every 1h"),
        "body": body,
        **fields,
        "outputs": [{"type": "file", "config": {"file": str(out / filename)}}],
    }
