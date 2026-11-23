"""OTLP request builders, a forwarder process wrapper and CloudWatch Logs readers.

The builders only encode public OTLP messages; expected EMF documents live in the tests.
"""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

import boto3
from google.protobuf import json_format
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2 as ms
from opentelemetry.proto.common.v1 import common_pb2 as cp
from opentelemetry.proto.metrics.v1 import metrics_pb2 as mp
from opentelemetry.proto.resource.v1 import resource_pb2 as rp

CUMULATIVE = mp.AGGREGATION_TEMPORALITY_CUMULATIVE
DELTA = mp.AGGREGATION_TEMPORALITY_DELTA


def now_ms():
    return time.time_ns() // 1_000_000


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def free_ports(count=2):
    sockets = [socket.socket() for _ in range(count)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


# ---- OTLP message builders -------------------------------------------------------------

def any_value(value):
    if isinstance(value, cp.AnyValue):
        return value
    if isinstance(value, bool):
        return cp.AnyValue(bool_value=value)
    if isinstance(value, int):
        return cp.AnyValue(int_value=value)
    if isinstance(value, float):
        return cp.AnyValue(double_value=value)
    if isinstance(value, bytes):
        return cp.AnyValue(bytes_value=value)
    if isinstance(value, (list, tuple)):
        return cp.AnyValue(array_value=cp.ArrayValue(values=[any_value(v) for v in value]))
    return cp.AnyValue(string_value=str(value))


def kvs(attributes):
    return [cp.KeyValue(key=k, value=any_value(v)) for k, v in (attributes or {}).items()]


def number_point(value, ts_ms, attributes=None, start_ms=None):
    point = mp.NumberDataPoint(attributes=kvs(attributes), time_unix_nano=ts_ms * 1_000_000,
                               start_time_unix_nano=(start_ms if start_ms is not None else ts_ms - 60_000) * 1_000_000)
    if isinstance(value, float):
        point.as_double = value
    elif isinstance(value, int):
        point.as_int = value
    return point


def gauge(name, points, unit=""):
    return mp.Metric(name=name, unit=unit, gauge=mp.Gauge(data_points=points))


def sum_metric(name, points, unit="", temporality=CUMULATIVE, monotonic=True):
    return mp.Metric(name=name, unit=unit, sum=mp.Sum(data_points=points, aggregation_temporality=temporality,
                                                      is_monotonic=monotonic))


def histogram_point(ts_ms, count, total=None, minimum=None, maximum=None, attributes=None,
                    bounds=(), buckets=()):
    point = mp.HistogramDataPoint(attributes=kvs(attributes), time_unix_nano=ts_ms * 1_000_000,
                                  start_time_unix_nano=(ts_ms - 60_000) * 1_000_000, count=count,
                                  explicit_bounds=list(bounds), bucket_counts=list(buckets))
    if total is not None:
        point.sum = total
    if minimum is not None:
        point.min = minimum
    if maximum is not None:
        point.max = maximum
    return point


def histogram(name, points, unit="", temporality=DELTA):
    return mp.Metric(name=name, unit=unit,
                     histogram=mp.Histogram(data_points=points, aggregation_temporality=temporality))


def summary_point(ts_ms, count, total, quantiles=(), attributes=None):
    return mp.SummaryDataPoint(
        attributes=kvs(attributes), time_unix_nano=ts_ms * 1_000_000, start_time_unix_nano=(ts_ms - 60_000) * 1_000_000,
        count=count, sum=total,
        quantile_values=[mp.SummaryDataPoint.ValueAtQuantile(quantile=q, value=v) for q, v in quantiles])


def summary(name, points, unit=""):
    return mp.Metric(name=name, unit=unit, summary=mp.Summary(data_points=points))


def resource_metrics(resource_attributes, metrics, scope="driver"):
    return mp.ResourceMetrics(resource=rp.Resource(attributes=kvs(resource_attributes)),
                              scope_metrics=[mp.ScopeMetrics(scope=cp.InstrumentationScope(name=scope),
                                                             metrics=list(metrics))])


def request(*resources):
    return ms.ExportMetricsServiceRequest(resource_metrics=list(resources))


def to_json(message):
    """OTLP/JSON for a request: lowerCamelCase names, integer enums, string times, numeric counts/asInt."""
    document = json_format.MessageToDict(message, preserving_proto_field_name=False, use_integers_for_enums=True)

    def fix(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("asInt", "intValue", "count") and isinstance(value, str):
                    node[key] = int(value)
                elif key == "bucketCounts":
                    node[key] = [int(v) for v in value]
                else:
                    fix(value)
        elif isinstance(node, list):
            for item in node:
                fix(item)

    fix(document)
    return json.dumps(document).encode()


# ---- Transports ------------------------------------------------------------------------

def post(port, body, content_type, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/metrics", data=body,
                                 headers={"Content-Type": content_type}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        response.read()
        return response.status


def send_protobuf(port, message):
    return post(port, message.SerializeToString(), "application/x-protobuf")


def send_json(port, message_or_bytes):
    body = message_or_bytes if isinstance(message_or_bytes, (bytes, str)) else to_json(message_or_bytes)
    return post(port, body.encode() if isinstance(body, str) else body, "application/json")


def send_grpc(port, message):
    import grpc
    from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2_grpc as msg

    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
        return msg.MetricsServiceStub(channel).Export(message, timeout=10)


# ---- The forwarder process ------------------------------------------------------------

def _default_launcher(argv, env, stdout, stderr):
    from harness import start_command

    return start_command(["/app/program", *argv], env=env, stdout=stdout, stderr=stderr, cwd="/")


class Forwarder:
    """Run `program start` with the awsemf exporter on free local ports."""

    def __init__(self, group=None, stream=None, extra=(), env=None, launcher=_default_launcher, endpoint_flags=True,
                 ports=None):
        self.http_port, self.grpc_port = ports or free_ports()
        argv = ["start"]
        if endpoint_flags:
            argv += ["--otlp-http-endpoint", f"127.0.0.1:{self.http_port}",
                     "--otlp-grpc-endpoint", f"127.0.0.1:{self.grpc_port}",
                     "--exporter", "awsemf",
                     "--awsemf-exporter-custom-endpoint", os.environ.get("AWS_ENDPOINT_URL", "http://engines:4566"),
                     "--awsemf-exporter-region", "us-east-1"]
        if group is not None:
            argv += ["--awsemf-exporter-log-group-name", group]
        if stream is not None:
            argv += ["--awsemf-exporter-log-stream-name", stream]
        self.argv = argv + list(extra)
        self.env = dict(env or {})
        self.launcher = launcher
        self.process = None
        self.returncode = None

    def __enter__(self):
        # Anonymous root-owned files: the candidate only receives the descriptors.
        self._out = tempfile.TemporaryFile()
        self._err = tempfile.TemporaryFile()
        self.process = self.launcher(self.argv, self.env, self._out, self._err)
        deadline = time.monotonic() + 15
        while True:
            if self.process.poll() is not None:
                self.stop()
                raise AssertionError(f"forwarder exited early with {self.returncode}: {self.output()}")
            try:
                socket.create_connection(("127.0.0.1", self.http_port), 0.2).close()
                socket.create_connection(("127.0.0.1", self.grpc_port), 0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    self.stop()
                    raise AssertionError(f"forwarder never listened on its OTLP ports: {self.output()}")
                time.sleep(0.05)
        return self

    def stop(self):
        if self.process is not None and self.returncode is None:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.returncode = self.process.returncode
        return self.returncode

    def __exit__(self, *exc):
        self.stop()

    def output(self):
        texts = []
        for handle in (self._out, self._err):
            handle.seek(0)
            texts.append(handle.read()[-3000:].decode(errors="replace"))
        return "stdout: %s\nstderr: %s" % tuple(texts)

    def protobuf(self, message):
        return send_protobuf(self.http_port, message)

    def json(self, message):
        return send_json(self.http_port, message)

    def grpc(self, message):
        return send_grpc(self.grpc_port, message)


# ---- CloudWatch Logs -------------------------------------------------------------------

def logs_client():
    return boto3.client("logs")


def read_events(group, stream):
    client = logs_client()
    try:
        events, token = [], None
        while True:
            kwargs = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
            if token:
                kwargs["nextToken"] = token
            page = client.get_log_events(**kwargs)
            events.extend(page["events"])
            if not page["events"] or page.get("nextForwardToken") in (None, token):
                break
            token = page.get("nextForwardToken")
        return events
    except client.exceptions.ResourceNotFoundException:
        return []


def wait_events(group, stream, count, timeout=25, settle=1.5, predicate=None):
    """Poll until at least `count` events (satisfying `predicate`) exist, then settle and re-read."""
    deadline = time.monotonic() + timeout
    events = []
    while time.monotonic() < deadline:
        events = read_events(group, stream)
        if len(events) >= count and (predicate is None or predicate(events)):
            break
        time.sleep(0.3)
    if settle:
        time.sleep(settle)
        events = read_events(group, stream)
    return events


def documents(events):
    return [json.loads(e["message"]) for e in events]


def delete_group(group):
    client = logs_client()
    try:
        client.delete_log_group(logGroupName=group)
    except client.exceptions.ResourceNotFoundException:
        pass


def describe_group(group):
    groups = logs_client().describe_log_groups(logGroupNamePrefix=group)["logGroups"]
    matches = [g for g in groups if g["logGroupName"] == group]
    return matches[0] if matches else None


def stream_names(group):
    client = logs_client()
    try:
        return sorted(s["logStreamName"] for s in client.describe_log_streams(logGroupName=group)["logStreams"])
    except client.exceptions.ResourceNotFoundException:
        return None
