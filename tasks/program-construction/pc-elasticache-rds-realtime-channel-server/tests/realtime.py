"""Shared helpers: Floci engines, a webhook sink, Pusher signing/clients and servers under test.

Expected values never come from here; this module only provisions fixtures and speaks the
public wire protocols (signed HTTP, Pusher WebSocket frames, Postgres rows, Valkey, SQS).
"""

import hashlib
import hmac
import http.client
import http.server
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import boto3
import psycopg
import redis
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

PG_USER, PG_PASS = "rtadmin", "rt-pass-123"
ALL_EVENTS = ["channel_occupied", "channel_vacated", "member_added", "member_removed", "client_event"]


def tag():
    return uuid.uuid4().hex[:10]


def wait_for(predicate, timeout=15.0, interval=0.1):
    """Return the first truthy predicate value, or the last (falsy) one after the timeout."""
    end = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < end:
        time.sleep(interval)
        value = predicate()
    return value


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── engines ─────────────────────────────────────────────────────────────────────


class Engines:
    """One RDS Postgres instance and one ElastiCache Valkey group for the whole session."""

    def __init__(self):
        self.rds = boto3.client("rds")
        self.ec = boto3.client("elasticache")
        self.name = "rt-" + tag()
        self.pg = None
        self.valkey = None

    def _rds(self):
        self.rds.create_db_instance(DBInstanceIdentifier=self.name, Engine="postgres", DBInstanceClass="db.t3.micro",
                                    MasterUsername=PG_USER, MasterUserPassword=PG_PASS, AllocatedStorage=20)
        while True:
            inst = self.rds.describe_db_instances(DBInstanceIdentifier=self.name)["DBInstances"][0]
            if inst.get("DBInstanceStatus") == "available" and inst.get("Endpoint"):
                return inst["Endpoint"]["Address"], int(inst["Endpoint"]["Port"])
            time.sleep(1)

    def _valkey(self):
        self.ec.create_replication_group(ReplicationGroupId=self.name, ReplicationGroupDescription="realtime tests",
                                         Engine="valkey", CacheNodeType="cache.t3.micro", NumCacheClusters=1)
        while True:
            rg = self.ec.describe_replication_groups(ReplicationGroupId=self.name)["ReplicationGroups"][0]
            if rg.get("Status") == "available":
                for group in rg.get("NodeGroups") or []:
                    if group.get("PrimaryEndpoint"):
                        return group["PrimaryEndpoint"]["Address"], int(group["PrimaryEndpoint"]["Port"])
            time.sleep(1)

    def start(self, timeout=400):
        with ThreadPoolExecutor(2) as pool:
            pg, vk = pool.submit(self._rds), pool.submit(self._valkey)
            self.pg = pg.result(timeout=timeout)
            self.valkey = vk.result(timeout=timeout)
        end = time.monotonic() + 120
        while True:
            try:
                with self.admin() as c:
                    c.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > end:
                    raise
                time.sleep(1)
        assert wait_for(lambda: self._ping(), 60)
        return self

    def _ping(self):
        try:
            return self.redis().ping()
        except redis.RedisError:
            return False

    def stop(self):
        for fn in (lambda: self.rds.delete_db_instance(DBInstanceIdentifier=self.name, SkipFinalSnapshot=True),
                   lambda: self.ec.delete_replication_group(ReplicationGroupId=self.name)):
            try:
                fn()
            except Exception:  # noqa: BLE001 -- best-effort teardown
                pass

    def admin(self, dbname="postgres"):
        return psycopg.connect(host=self.pg[0], port=self.pg[1], user=PG_USER, password=PG_PASS, dbname=dbname,
                               autocommit=True, connect_timeout=10)

    def redis(self):
        return redis.Redis(host=self.valkey[0], port=self.valkey[1], socket_timeout=10)

    @property
    def redis_url(self):
        return f"redis://{self.valkey[0]}:{self.valkey[1]}/0"


# ── webhook sink ────────────────────────────────────────────────────────────────


class Sink:
    """Records every webhook POST; `statuses` (a deque) scripts the response codes, default 200."""

    def __init__(self):
        self.requests = []
        self.statuses = deque()
        self.lock = threading.Lock()
        sink = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with sink.lock:
                    sink.requests.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                          "body": body, "at": time.monotonic()})
                    status = sink.statuses.popleft() if sink.statuses else 200
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def url(self, path="/hook"):
        return self.base + path

    def snapshot(self, path=None):
        with self.lock:
            return [r for r in self.requests if path is None or r["path"] == path]

    def events(self, path=None):
        return [e for r in self.snapshot(path) for e in json.loads(r["body"])["events"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ── Pusher signing ──────────────────────────────────────────────────────────────


def hmac_hex(secret, message):
    if isinstance(message, str):
        message = message.encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def channel_auth(app, socket_id, channel, channel_data=None):
    text = f"{socket_id}:{channel}" + (f":{channel_data}" if channel_data is not None else "")
    return f"{app.key}:{hmac_hex(app.secret, text)}"


def signed_query(app, method, path, body=None, params=None, timestamp=None, key=None, secret=None, md5=True):
    query = dict(params or {})
    query.update(auth_key=key or app.key, auth_timestamp=str(int(time.time()) if timestamp is None else timestamp), auth_version="1.0")
    if body is not None and md5:
        query["body_md5"] = hashlib.md5(body).hexdigest()
    to_sign = "&".join(f"{k}={query[k]}" for k in sorted(query))
    query["auth_signature"] = hmac_hex(secret or app.secret, f"{method}\n{path}\n{to_sign}")
    return query


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        return json.loads(self.body)

    def __repr__(self):
        return f"<{self.status} {self.body[:300]!r}>"


# ── apps ───────────────────────────────────────────────────────────────────────


class App:
    def __init__(self, id, key, secret):
        self.id, self.key, self.secret = id, key, secret


def insert_app(conn, app, *, webhooks=None, max_connections=100, enable_client_messages=True, enabled=True,
               max_client_events_per_second=100, table="applications", **columns):
    cols = {"id": app.id, "key": app.key, "secret": app.secret, "max_connections": max_connections,
            "enable_client_messages": enable_client_messages, "enabled": enabled,
            "max_client_events_per_second": max_client_events_per_second, **columns}
    if webhooks is not None:
        cols["webhooks"] = psycopg.types.json.Jsonb(webhooks)
    names = ", ".join(f'"{k}"' for k in cols)
    marks = ", ".join(["%s"] * len(cols))
    conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', list(cols.values()))


# ── deployment and servers ─────────────────────────────────────────────────────


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def render_toml(top, sections):
    lines = [f"{k} = {toml_value(v)}" for k, v in top.items()]
    for name, values in sections.items():
        lines.append(f"\n[{name}]")
        lines.extend(f"{k} = {toml_value(v)}" for k, v in values.items())
    return "\n".join(lines) + "\n"


class Deployment:
    """A Postgres database, an SQS webhook queue and Valkey prefixes shared by one or more servers."""

    def __init__(self, engines, workdir, launcher, program, *, fifo=False):
        self.engines, self.workdir, self.launcher, self.program = engines, workdir, launcher, program
        self.tag = tag()
        self.db = "rt_" + self.tag
        self.sqs = boto3.client("sqs")
        self.servers = []
        self.sink = Sink()
        self.adapter_prefix = "rt" + self.tag
        self.cache_prefix = "rtc" + self.tag + ":"
        with engines.admin() as c:
            c.execute(f'CREATE DATABASE "{self.db}"')
        name = f"rt-{self.tag}-webhooks" + (".fifo" if fifo else "")
        attrs = {"VisibilityTimeout": "30"}
        if fifo:
            attrs.update(FifoQueue="true", ContentBasedDeduplication="true")
        self.queue_url = self.sqs.create_queue(QueueName=name, Attributes=attrs)["QueueUrl"]
        self.queue_prefix = self.queue_url[: self.queue_url.rindex("webhooks")]

    def conn(self):
        return self.engines.admin(self.db)

    def app(self, suffix="", **row):
        unique = suffix + tag()
        app = App(f"app{unique}", f"key{unique}", f"secret{unique}")
        with self.conn() as c:
            insert_app(c, app, **row)
        return app

    def config(self, port, *, sections=None, top=None):
        base = {
            "app_manager": {"driver": "pgsql"},
            "database.postgres": {"host": self.engines.pg[0], "port": self.engines.pg[1], "username": PG_USER,
                                  "password": PG_PASS, "database": self.db, "table_name": "applications"},
            "adapter": {"driver": "redis"},
            "adapter.redis": {"prefix": self.adapter_prefix},
            "cache": {"driver": "redis"},
            "cache.redis": {"prefix": self.cache_prefix},
            "queue": {"driver": "sqs"},
            "queue.sqs": {"region": "us-east-1", "endpoint_url": os.environ.get("AWS_ENDPOINT_URL", "http://engines:4566"),
                          "queue_url_prefix": self.queue_prefix, "visibility_timeout": 30, "wait_time_seconds": 1},
            "metrics": {"enabled": False},
            "rate_limiter": {"enabled": False},
        }
        for name, values in (sections or {}).items():
            if values is None:
                base.pop(name, None)
            else:
                base[name] = {**base.get(name, {}), **values}
        return render_toml({"host": "127.0.0.1", "port": port, **(top or {})}, base)

    @property
    def server(self):
        return self.servers[0]

    def start(self, *, sections=None, top=None, env=None, wait=True, args=None, config_text=None, port=None, files=None):
        server = Server(self, sections=sections, top=top, env=env, args=args, config_text=config_text, port=port, files=files)
        self.servers.append(server)
        if wait:
            server.wait_listening()
        return server

    def queue_counts(self):
        attrs = self.sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return int(attrs["ApproximateNumberOfMessages"]), int(attrs["ApproximateNumberOfMessagesNotVisible"])

    def close(self):
        for server in self.servers:
            server.stop()
        self.sink.close()
        try:
            self.sqs.delete_queue(QueueUrl=self.queue_url)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = self.engines.redis()
            for pattern in (self.adapter_prefix + "*", self.cache_prefix + "*"):
                for key in r.scan_iter(match=pattern, count=500):
                    r.delete(key)
        except Exception:  # noqa: BLE001
            pass
        try:
            with self.engines.admin() as c:
                c.execute(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)')
        except Exception:  # noqa: BLE001
            pass


class Server:
    def __init__(self, deployment, *, sections=None, top=None, env=None, args=None, config_text=None, port=None, files=None):
        """`port` is where the server is expected to listen; `files` are placed in its working directory."""
        self.deployment = deployment
        self.port = port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.dir = os.path.join(deployment.workdir, f"node-{self.port}-{tag()[:4]}")
        os.mkdir(self.dir)
        os.chmod(self.dir, 0o755)
        for name, text in (files or {}).items():
            path = os.path.join(self.dir, name)
            os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)
            os.chmod(path, 0o644)
        self.config_path = os.path.join(self.dir, "config.toml")
        with open(self.config_path, "w") as fh:
            fh.write(config_text if config_text is not None else deployment.config(self.port, sections=sections, top=top))
        os.chmod(self.config_path, 0o644)
        self.log_path = self.dir + ".log"
        self.log = open(self.log_path, "wb")  # noqa: SIM115
        full_env = {"REDIS_URL": deployment.engines.redis_url, "SHUTDOWN_GRACE_PERIOD": "1", "LOG_COLORS_ENABLED": "false",
                    **(env or {})}
        if os.geteuid() == 0:
            os.chown(self.dir, 2000, 2000)
        argv = [deployment.program, *(args if args is not None else ["--config", self.config_path])]
        self.process = deployment.launcher(argv, cwd=self.dir, env=full_env, stdout=self.log, stderr=subprocess.STDOUT)

    def up(self, path="/up"):
        try:
            return self.request("GET", path, timeout=3)
        except OSError:
            return None

    def wait_listening(self, timeout=40):
        def ready():
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}: {self.log_text()[-3000:]}")
            return self.up()
        assert wait_for(ready, timeout, 0.2), "server did not start listening"

    def wait_healthy(self, timeout=20):
        assert wait_for(lambda: (r := self.up()) is not None and r.status == 200, timeout, 0.2), "server never became healthy"

    def log_text(self):
        self.log.flush()
        with open(self.log_path, "rb") as fh:
            return fh.read().decode(errors="replace")

    def request(self, method, target, body=None, headers=None, timeout=10):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, target, body=body, headers=headers or {})
            resp = conn.getresponse()
            return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read())
        finally:
            conn.close()

    def api(self, app, method, path, body=None, params=None, raw=None, content_type="application/json", **sign):
        """Signed API call. `body` is JSON-encoded; `raw` gives exact bytes."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if method == "POST" and data is None:
            data = b""
        query = signed_query(app, method, path, data if method == "POST" else None, params, **sign)
        target = path + "?" + urllib.parse.urlencode(query)
        headers = {"Content-Type": content_type} if data is not None else {}
        return self.request(method, target, body=data, headers=headers)

    def ws(self, app_key, query="protocol=7&client=py&version=1.0"):
        return Client(f"ws://127.0.0.1:{self.port}/app/{app_key}?{query}")

    def connect(self, app):
        """An established connection for `app`."""
        client = self.ws(app.key)
        frame = client.handshake()
        assert frame is not None and frame.get("event") == "pusher:connection_established", frame
        return client

    def stop(self, timeout=20):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.log.close()


# ── WebSocket client ───────────────────────────────────────────────────────────


class Client:
    """A Pusher-protocol client that keeps every received frame for later assertions."""

    def __init__(self, url):
        self.url = url
        self.frames = []
        self.used = set()
        self.closed = None
        self.conn = ws_connect(url, open_timeout=10, close_timeout=2, max_size=None)
        self.established = None

    def _pull(self, timeout):
        if self.closed is not None:
            return False
        try:
            raw = self.conn.recv(timeout=timeout)
        except TimeoutError:
            return False
        except ConnectionClosed as error:
            rcvd = error.rcvd
            self.closed = (rcvd.code, rcvd.reason) if rcvd is not None else (None, None)
            return False
        self.frames.append(json.loads(raw))
        return True

    def handshake(self, timeout=10):
        frame = self.expect(lambda f: True, timeout)
        self.established = frame
        if frame and frame.get("event") == "pusher:connection_established":
            self.socket_id = json.loads(frame["data"])["socket_id"]
        return frame

    def expect(self, predicate, timeout=10):
        """Return the first unconsumed frame matching predicate; frames before it stay pending."""
        end = time.monotonic() + timeout
        index = 0
        while True:
            while index < len(self.frames):
                if index not in self.used and predicate(self.frames[index]):
                    self.used.add(index)
                    return self.frames[index]
                index += 1
            remaining = end - time.monotonic()
            if remaining <= 0 or self.closed is not None:
                return None
            self._pull(min(remaining, 0.5))

    def event(self, name, channel=None, timeout=10):
        return self.expect(lambda f: f.get("event") == name and (channel is None or f.get("channel") == channel), timeout)

    def pending(self, wait=0.0):
        """Frames received and not consumed by expect(), after reading for `wait` seconds."""
        end = time.monotonic() + wait
        while time.monotonic() < end and self._pull(max(0.01, end - time.monotonic())):
            pass
        return [f for i, f in enumerate(self.frames) if i not in self.used]

    def consume_all(self):
        self.used.update(range(len(self.frames)))

    def wait_closed(self, timeout=10):
        end = time.monotonic() + timeout
        while self.closed is None and time.monotonic() < end:
            self._pull(min(0.5, end - time.monotonic()))
        return self.closed

    def send(self, frame):
        self.conn.send(frame if isinstance(frame, str) else json.dumps(frame))

    def subscribe(self, channel, auth=None, channel_data=None):
        data = {"channel": channel}
        if auth is not None:
            data["auth"] = auth
        if channel_data is not None:
            data["channel_data"] = channel_data
        self.send({"event": "pusher:subscribe", "data": data})

    def join(self, channel, app=None, user_id=None, user_info=None, timeout=10):
        """Subscribe (signing private/presence channels for `app`) and return subscription_succeeded."""
        auth = data = None
        if channel.startswith("presence-"):
            member = {"user_id": user_id}
            if user_info is not None:
                member["user_info"] = user_info
            data = json.dumps(member)
            auth = channel_auth(app, self.socket_id, channel, data)
        elif channel.startswith("private-"):
            auth = channel_auth(app, self.socket_id, channel)
        self.subscribe(channel, auth, data)
        frame = self.expect(lambda f: f.get("channel") == channel or f.get("event") == "pusher:error", timeout)
        assert frame is not None and frame.get("event") == "pusher_internal:subscription_succeeded", f"join {channel}: {frame}"
        return frame

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


# ── comparisons ────────────────────────────────────────────────────────────────


def same(a, b):
    """JSON equality that keeps booleans distinct from numbers; numbers compare by value."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def same_multiset(actual, expected):
    """Unordered comparison that keeps multiplicity."""
    remaining = list(expected)
    for item in actual:
        for i, candidate in enumerate(remaining):
            if same(item, candidate):
                del remaining[i]
                break
        else:
            return False
    return not remaining


def decoded(frame):
    """A frame with its JSON-string `data` decoded (the data must be a string)."""
    assert isinstance(frame.get("data"), str), frame
    return {**frame, "data": json.loads(frame["data"])}
