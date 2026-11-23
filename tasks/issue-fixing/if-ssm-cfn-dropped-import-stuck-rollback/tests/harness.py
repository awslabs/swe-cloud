"""Verifier harness: builds producer/consumer templates, drives deploy schedules on the verifier's
own copy of the engine, probes the strong references after every resource response, and records
checkpoints.  Nothing here imports code from /app; the handlers under test are only ever run as
processes by the engine."""
import json
import os
import secrets
import tempfile
import traceback

import boto3
from botocore.exceptions import ClientError

from stackengine import DEFAULT_TABLE, Engine, Sandbox

LOG_DIR = os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier")
APP_DIR = os.environ.get("VERIFIER_APP_DIR", "/app")
# the handlers /app/crossref shipped with before the fix, used to deploy the state the fix takes over
LEGACY_APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy")
REGION = "us-east-1"
WRITER_PROVIDER = "CustomCrossRegionExportWriterCustomResourceProviderHandlerD8786E8A"
READER_PROVIDER = "CustomCrossRegionExportReaderCustomResourceProviderHandler46647B68"
_ASSETS = None


def asset_dir():
    """One asset store for the session (handlers read their code from it as the handler user)."""
    global _ASSETS
    if _ASSETS is None:
        _ASSETS = tempfile.mkdtemp(prefix="verifier-assets-")
        os.chmod(_ASSETS, 0o755)
    return _ASSETS

OK_CHECK = ["python3", "-c", "pass"]


def failing_check(message):
    return ["python3", "-c", f"import sys; print({message!r}, file=sys.stderr); sys.exit(1)"]


def ssm_client():
    return boto3.client("ssm", region_name=REGION)


def read_param(ssm, name):
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def reset_table():
    ddb = boto3.client("dynamodb", region_name=REGION)
    try:
        ddb.delete_table(TableName=DEFAULT_TABLE)
        ddb.get_waiter("table_not_exists").wait(TableName=DEFAULT_TABLE, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    ddb.create_table(
        TableName=DEFAULT_TABLE,
        AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.get_waiter("table_exists").wait(TableName=DEFAULT_TABLE, WaiterConfig={"Delay": 1, "MaxAttempts": 30})


class Names:
    """Randomised stack names, parameter names and values for one scenario."""

    def __init__(self, producer_stem, consumer_stem):
        self.producer = f"{producer_stem}{secrets.token_hex(3)}"
        self.consumer = f"{consumer_stem}{secrets.token_hex(3)}"
        self.writer_id = f"ExportsWriteruseast1{secrets.token_hex(4).upper()}"
        self.reader_id = f"ExportsReader{secrets.token_hex(4).upper()}"

    def param(self, construct, attribute="Ref"):
        return (f"/cdk/exports/{self.consumer}/{self.producer}useast1"
                f"{attribute}{construct}{secrets.token_hex(4).upper()}{secrets.token_hex(4).upper()}")

    @staticmethod
    def value(kind):
        return {
            "sg": lambda: f"sg-0{secrets.token_hex(8)}",
            "arn": lambda: f"arn:aws:sns:us-east-1:000000000000:topic-{secrets.token_hex(5)}",
            "url": lambda: f"https://sqs.us-east-1.amazonaws.com/000000000000/q-{secrets.token_hex(5)}",
        }[kind]()

    def producer_template(self, exports, tail_check=None):
        return {"Resources": {
            WRITER_PROVIDER: {"Type": "Local::Function", "Properties": {
                "Handler": "crossref.writer", "Code": {"Asset": "crossref"}}},
            self.writer_id: {"Type": "Custom::CrossRegionExportWriter", "Properties": {
                "ServiceToken": {"Fn::GetAtt": [WRITER_PROVIDER, "Arn"]},
                "WriterProps": {"region": REGION, "exports": dict(exports)}}},
            "ProducerTail": {"Type": "Local::Hook", "Properties": {"Command": tail_check or OK_CHECK}},
        }}

    def consumer_template(self, names, health_check=None):
        names = list(names)
        return {"Resources": {
            READER_PROVIDER: {"Type": "Local::Function", "Properties": {
                "Handler": "crossref.reader", "Code": {"Asset": "crossref"}}},
            self.reader_id: {"Type": "Custom::CrossRegionExportReader", "Properties": {
                "ServiceToken": {"Fn::GetAtt": [READER_PROVIDER, "Arn"]},
                "ReaderProps": {"region": REGION, "prefix": self.consumer,
                                "imports": {n: "{{resolve:ssm:%s}}" % n for n in names}}}},
            "App": {"Type": "Local::Service", "Properties": {
                "Environment": {f"SETTING_{i:02d}": "{{resolve:ssm:%s}}" % n for i, n in enumerate(names)},
                "HealthCheck": health_check or OK_CHECK}},
        }}


class Probe:
    """After every resource response: every parameter the consumer's committed state imports, and
    every parameter its in-flight target state has already resolved, exists with the value that
    state captured."""

    def __init__(self, consumers):
        self.consumers = set(consumers)
        self.ssm = ssm_client()
        self.violations = []
        self.checks = 0
        self.strays = []

    def __call__(self, engine, event):
        if event["kind"] != "response":
            return
        if event.get("strays"):
            self.strays.append({"seq": event["seq"], "stack": event["stack"], "logical": event["logical"],
                                "strays": event["strays"]})
        held = []
        for name in self.consumers:
            stack = engine.stacks.get(name)
            if stack:
                for rec in stack["resources"].values():
                    held += [("committed", n, v) for n, v in rec["refs"].items()]
        if engine.inflight and engine.inflight["stack"] in self.consumers:
            for rec in engine.inflight["applied"].values():
                held += [("in-flight", n, v) for n, v in rec["refs"].items()]
        for state, name, expected in sorted(set(held)):
            self.checks += 1
            actual = read_param(self.ssm, name)
            if actual != expected:
                self.violations.append({
                    "after_seq": event["seq"], "after": f"{event['stack']}/{event['logical']} {event['request_type']}"
                    f" -> {event['response'].get('Status')}", "state": state, "name": name,
                    "expected": expected, "actual": actual})


class Scenario:
    def __init__(self, label, names):
        self.label = label
        self.n = names
        self.probe = Probe([names.consumer])
        self.ssm = ssm_client()
        self.engine = Engine(sandbox=Sandbox(app_dir=APP_DIR, table=DEFAULT_TABLE,
                                             log_dir=os.path.join(LOG_DIR, "handler-logs", label)),
                             observer=self.probe, region=REGION, source_dir=APP_DIR, asset_dir=asset_dir())
        self.checkpoints = {}
        self.error = None

    def deploy(self, stack, template):
        return self.engine.deploy(stack, template)

    def handlers(self, app_dir):
        """Deploy the handler code in ``app_dir`` from now on (the stacks stay as they are; each stack
        gets that code with its next deploy, as its functions' code asset)."""
        self.engine.source_dir = app_dir

    def checkpoint(self, label, names):
        self.checkpoints[label] = {
            "producer": (self.engine.stacks.get(self.n.producer) or {}).get("status"),
            "consumer": (self.engine.stacks.get(self.n.consumer) or {}).get("status"),
            "params": {name: read_param(self.ssm, name) for name in names},
            "violations_so_far": len(self.probe.violations),
        }

    def run(self, steps):
        try:
            steps(self)
        except Exception:  # noqa: BLE001 - recorded, every test reports it
            self.error = traceback.format_exc()
        finally:
            self.dump()
        return self

    def dump(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"journal-{self.label}.jsonl"), "w") as fh:
            for event in self.engine.journal:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        with open(os.path.join(LOG_DIR, f"summary-{self.label}.json"), "w") as fh:
            json.dump({"producer": self.n.producer, "consumer": self.n.consumer,
                       "checkpoints": self.checkpoints, "violations": self.probe.violations,
                       "probe_checks": self.probe.checks, "strays": self.probe.strays,
                       "error": self.error}, fh, indent=1, sort_keys=True)

    def describe(self, label=None):
        """Context for an assertion message."""
        parts = [f"scenario {self.label}"]
        if self.error:
            parts.append(f"harness error:\n{self.error}")
        if label:
            parts.append(f"{label}: {json.dumps(self.checkpoints.get(label), indent=1)}")
        reasons = [e for e in self.engine.journal if e["kind"] == "status" and e.get("reason")]
        parts.append("failure reasons: " + json.dumps([f"{e['stack']} {e['status']}: {e['reason'][:300]}"
                                                       for e in reasons[-6:]], indent=1))
        return "\n".join(parts)
