"""Shared test helpers: run the candidate server, call its API, observe AWS and stores."""

import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import psycopg
import redis

from harness import VENUE, run_program, start_command

ENDPOINT = VENUE["AWS_ENDPOINT_URL"]
PG_USER, PG_PASSWORD = "hub", "hub-pass-1"
CREDS = {"key": "test", "secret": "test"}
TOPICS = "user.created,user.updated,user.deleted"

sqs = boto3.client("sqs")
eventbridge = boto3.client("events")


def uid():
    return uuid.uuid4().hex[:10]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Engines:
    """Session-wide PostgreSQL instance and Valkey replication group."""

    def __init__(self, pg_host, pg_port, kv_host, kv_port):
        self.pg_host, self.pg_port, self.kv_host, self.kv_port = pg_host, pg_port, kv_host, kv_port

    def admin(self):
        return psycopg.connect(host=self.pg_host, port=self.pg_port, user=PG_USER, password=PG_PASSWORD,
                               dbname="hub", sslmode="disable", autocommit=True, connect_timeout=10)

    def new_database(self):
        name = "db_" + uid()
        with self.admin() as conn:
            conn.execute(f'CREATE DATABASE "{name}"')
        return name

    def drop_database(self, name):
        with self.admin() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def url(self, database):
        return f"postgres://{PG_USER}:{PG_PASSWORD}@{self.pg_host}:{self.pg_port}/{database}?sslmode=disable"

    def pg(self, database):
        return psycopg.connect(host=self.pg_host, port=self.pg_port, user=PG_USER, password=PG_PASSWORD,
                               dbname=database, sslmode="disable", autocommit=True, connect_timeout=10)

    def valkey(self):
        return redis.Redis(host=self.kv_host, port=self.kv_port)


def provision_engines():
    rds, ec = boto3.client("rds"), boto3.client("elasticache")
    run = uid()
    db_id, rg_id = f"hub-db-{run}", f"hub-kv-{run}"
    cleanup = []
    try:
        rds.create_db_instance(DBInstanceIdentifier=db_id, Engine="postgres", DBInstanceClass="db.t3.micro", DBName="hub",
                               MasterUsername=PG_USER, MasterUserPassword=PG_PASSWORD, AllocatedStorage=20)
        cleanup.append(lambda: rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True))
        ec.create_replication_group(ReplicationGroupId=rg_id, ReplicationGroupDescription="hub tests", Engine="valkey",
                                    CacheNodeType="cache.t3.micro", NumCacheClusters=1)
        cleanup.append(lambda: ec.delete_replication_group(ReplicationGroupId=rg_id))
        rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=db_id, WaiterConfig={"Delay": 2, "MaxAttempts": 150})
        endpoint = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]["Endpoint"]
        deadline = time.time() + 300
        while True:
            group = ec.describe_replication_groups(ReplicationGroupId=rg_id)["ReplicationGroups"][0]
            if group.get("Status") == "available":
                kv = group["NodeGroups"][0]["PrimaryEndpoint"]
                break
            if time.time() > deadline:
                raise RuntimeError("Valkey replication group did not become available")
            time.sleep(2)
        engines = Engines(endpoint["Address"], endpoint["Port"], kv["Address"], kv["Port"])
        deadline = time.time() + 120
        while True:
            try:
                engines.admin().close()
                engines.valkey().ping()
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(2)
        return engines, cleanup
    except BaseException:
        for step in reversed(cleanup):
            try:
                step()
            except Exception:
                pass
        raise


class Response:
    def __init__(self, status, raw):
        self.status, self.raw = status, raw
        try:
            self.json = json.loads(raw) if raw else None
        except ValueError:
            self.json = None

    def __repr__(self):
        return f"<{self.status} {self.raw[:600]!r}>"


class Hub:
    """One candidate server configuration: its environment, stores and internal queues."""

    def __init__(self, engines, **settings):
        self.engines = engines
        self.id = uid()
        self.port = free_port()
        self.database = engines.new_database()
        self.deployment = "d" + self.id
        self.api_key = "key-" + self.id
        self.jwt_secret = "jwt-secret-" + self.id
        self.delivery_queue, self.log_queue = f"hub-dlv-{self.id}", f"hub-log-{self.id}"
        self.env = {
            "DISABLE_TELEMETRY": "true",
            "API_KEY": self.api_key,
            "API_JWT_SECRET": self.jwt_secret,
            "AES_ENCRYPTION_SECRET": "0123456789abcdef0123456789abcdef",
            "API_PORT": str(self.port),
            "REDIS_HOST": engines.kv_host,
            "REDIS_PORT": str(engines.kv_port),
            "REDIS_PASSWORD": "",
            "REDIS_DATABASE": "0",
            "POSTGRES_URL": engines.url(self.database),
            "MQS_AUTO_PROVISION": "true",
            "AWS_SQS_REGION": "us-east-1",
            "AWS_SQS_ENDPOINT": ENDPOINT,
            "AWS_SQS_ACCESS_KEY_ID": "test",
            "AWS_SQS_SECRET_ACCESS_KEY": "test",
            "AWS_SQS_DELIVERY_QUEUE": self.delivery_queue,
            "AWS_SQS_LOG_QUEUE": self.log_queue,
            "DEPLOYMENT_ID": self.deployment,
            "LOG_BATCH_THRESHOLD_SECONDS": "1",
            "LOG_LEVEL": "info",
            "TOPICS": TOPICS,
        }
        for key, value in settings.items():
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = str(value)
        self.deployment = self.env.get("DEPLOYMENT_ID", "")
        self.proc = None
        self.log = None

    # -- process control -------------------------------------------------
    def cli(self, *args, timeout=60, stdin=None):
        return run_program(list(args), env=self.env, timeout=timeout, stdin=stdin)

    def migrate(self):
        result = self.cli("migrate", "apply", "--yes")
        assert result.returncode == 0, f"migrate apply failed: {result.stdout}\n{result.stderr}"

    def start(self, migrate=True):
        if migrate:
            self.migrate()
        self.log = tempfile.NamedTemporaryFile(prefix="hub-server-", suffix=".log", delete=False)
        self.proc = start_command(["/app/program", "serve"], env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1) as r:
                    if r.status == 200:
                        return self
            except (OSError, urllib.error.URLError):
                pass
            if self.proc.poll() is not None:
                raise AssertionError(f"server exited with {self.proc.returncode}: {self.server_log()}")
            if time.time() > deadline:
                raise AssertionError(f"server not healthy within 30s: {self.server_log()}")
            time.sleep(0.2)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except Exception:
                self.proc.kill()
                self.proc.wait(10)
        self.proc = None

    def server_log(self, limit=12000):
        if self.log is None:
            return ""
        self.log.flush()
        with open(self.log.name, "rb") as f:
            return f.read()[-limit:].decode(errors="replace")

    def close(self):
        """Stop the server and remove its internal queues, database and Valkey namespace."""
        try:
            self.stop()
        finally:
            for name in (self.delivery_queue, self.log_queue, self.delivery_queue + "-dlq", self.log_queue + "-dlq"):
                try:
                    sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=name)["QueueUrl"])
                except Exception:
                    pass
            try:
                self.engines.drop_database(self.database)
            except Exception:
                pass
            if self.deployment:
                try:
                    kv = self.engines.valkey()
                    keys = list(kv.scan_iter(match=self.deployment + ":*", count=1000))
                    if keys:
                        kv.delete(*keys)
                except Exception:
                    pass
            if self.log is not None:
                self.log.close()
                os.unlink(self.log.name)
                self.log = None

    # -- API ---------------------------------------------------------------
    def request(self, method, path, body=None, raw=None, auth="admin", base="/api/v1"):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {"Content-Type": "application/json"}
        if auth == "admin":
            headers["Authorization"] = "Bearer " + self.api_key
        elif auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{base}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return Response(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read())

    def ok(self, method, path, body=None, status=200, **kw):
        r = self.request(method, path, body, **kw)
        assert r.status == status, f"{method} {path}: expected {status}, got {r}"
        return r.json

    def tenant(self, metadata=None):
        tid = "t-" + uid()
        self.ok("PUT", f"/tenants/{tid}", {} if metadata is None else {"metadata": metadata}, status=201)
        return tid

    def sqs_destination(self, tenant, queue_url, topics=("user.created",), **fields):
        body = {"type": "aws_sqs", "topics": topics if isinstance(topics, str) else list(topics),
                "config": {"queue_url": queue_url, "endpoint": ENDPOINT}, "credentials": dict(CREDS), **fields}
        return self.ok("POST", f"/tenants/{tenant}/destinations", body, status=201)

    def publish(self, body, status=202):
        return self.ok("POST", "/publish", body, status=status)

    def attempts(self, tenant, query=""):
        return self.ok("GET", f"/attempts?tenant_id={tenant}&dir=asc&limit=1000{query}")["models"]

    def wait_attempts(self, tenant, count, query="", timeout=20):
        deadline = time.time() + timeout
        while True:
            models = self.attempts(tenant, query)
            if len(models) >= count or time.time() > deadline:
                return models
            time.sleep(0.5)

    def wait_events(self, tenant, count, timeout=20):
        deadline = time.time() + timeout
        while True:
            models = self.ok("GET", f"/events?tenant_id={tenant}&dir=asc&limit=1000")["models"]
            if len(models) >= count or time.time() > deadline:
                return models
            time.sleep(0.5)

    # -- stores ------------------------------------------------------------
    def key(self, suffix):
        return f"{self.deployment}:{suffix}" if self.deployment else suffix

    def pg(self):
        return self.engines.pg(self.database)


class Queues:
    """Test-owned SQS queues, removed at teardown."""

    def __init__(self):
        self.urls = []

    def create(self, prefix="q", **attributes):
        name = f"{prefix}-{uid()}"
        url = sqs.create_queue(QueueName=name, Attributes={k: str(v) for k, v in attributes.items()})["QueueUrl"]
        self.urls.append(url)
        return url

    def missing(self):
        """URL of a queue that does not exist (deliveries to it fail)."""
        probe = self.create("base")
        return probe.rsplit("/", 1)[0] + "/absent-" + uid()

    def arn(self, url):
        return sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    def cleanup(self):
        for url in self.urls:
            try:
                sqs.delete_queue(QueueUrl=url)
            except Exception:
                pass


def receive(url, count, timeout=15):
    """Collect and delete up to `count` messages, waiting up to `timeout` seconds."""
    messages, deadline = [], time.time() + timeout
    while len(messages) < count and time.time() < deadline:
        r = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                MessageAttributeNames=["All"], AttributeNames=["All"])
        for m in r.get("Messages", []):
            messages.append(m)
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return messages


def assert_quiet(url, wait=2):
    extra = receive(url, 1, timeout=wait)
    assert not extra, f"unexpected extra message on {url}: {[m['Body'] for m in extra]}"


def metadata_attribute(message):
    attrs = message.get("MessageAttributes") or {}
    assert set(attrs) == {"metadata"}, f"message attributes: {attrs}"
    assert attrs["metadata"]["DataType"] == "String"
    return json.loads(attrs["metadata"]["StringValue"])


def parse_time(value):
    """An RFC 3339 timestamp with an explicit zone, any fractional precision (kept to microseconds)."""
    assert isinstance(value, str), value
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", value)
    assert m, f"not an RFC 3339 timestamp: {value!r}"
    fraction = ((m.group(2) or "") + "000000")[:6]
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}.{fraction}{zone}").astimezone(timezone.utc)


def millis(value):
    t = parse_time(value)
    return int(t.timestamp()) * 1000 + t.microsecond // 1000


def recent(value, start, slack=5):
    """The timestamp lies between `start` (epoch seconds) and now, give or take `slack` seconds."""
    t = parse_time(value).timestamp()
    assert start - slack <= t <= time.time() + slack, f"{value} not within the test window"
    return t


def same(actual, expected, path="$"):
    """JSON equality: numbers by value, booleans distinct from numbers, order kept in arrays."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        assert type(actual) is bool and type(expected) is bool and actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: {actual!r} is not an object"
        assert set(actual) == set(expected), f"{path}: keys {sorted(actual)} != {sorted(expected)}"
        for k in expected:
            same(actual[k], expected[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{path}: {actual!r} != {expected!r}"
        for i, (a, e) in enumerate(zip(actual, expected)):
            same(a, e, f"{path}[{i}]")
    else:
        assert type(actual) is type(expected) and actual == expected, f"{path}: {actual!r} != {expected!r}"


def model(obj, expected, times=()):
    """Compare an API model: absent and null members are equivalent; listed time members are skipped."""
    present = {k: v for k, v in obj.items() if v is not None and k not in times}
    wanted = {k: v for k, v in expected.items() if v is not None and k not in times}
    same(present, wanted)


def stored_number(value):
    """A numeric field stored in Valkey (bytes or text), compared by value: 1577836800000 == 1577836800000.0."""
    text = value.decode() if isinstance(value, bytes) else value
    number = Decimal(text.strip())
    assert number.is_finite(), value
    return number


def same_model(actual, expected):
    """Two API resource objects: top-level absent and null members are equivalent."""
    same({k: v for k, v in actual.items() if v is not None}, {k: v for k, v in expected.items() if v is not None})


def ids(values):
    """An unordered collection of IDs, multiplicity kept."""
    assert isinstance(values, list), values
    return sorted(values)


def error(response, status, message, data=None):
    assert response.status == status, f"expected {status}: {response}"
    body = response.json
    assert isinstance(body, dict), response
    expected = {"status": status, "message": message}
    if data is not None:
        expected["data"] = data
    same({k: v for k, v in body.items() if v is not None}, expected)
