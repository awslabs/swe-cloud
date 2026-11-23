"""Shared fixtures: Floci RDS Postgres + ElastiCache Valkey, stub upstreams, and one running /app/program.

The server is started once per session as the candidate. Channels and options are provisioned once at
start (they are re-read every SYNC_FREQUENCY seconds); every test then creates its own users and tokens,
so tests are isolated by user id, token key and a unique prompt tag recorded by the stub upstreams.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import psycopg
import pytest
import redis

from harness import give_to_candidate, start_command, workspace

SYNC = 5  # SYNC_FREQUENCY given to the program: cache TTL and channel/option refresh period
MODEL = "gpt-3.5-turbo"
KV_DB = 3
GROUP_RATIO = {"default": 1, "vip": 1, "svip": 1, "pair": 1, "tier": 1, "flat": 1, "alpha": 1, "beta": 1, "double": 2, "nochan": 1, "negtier": 1,
               "multi": 1, "multi3": 3}
# name -> (groups, priority, status, weight)
CHANNELS = {
    "vip": ("vip", 0, 1, 0),
    "svip": ("svip", 0, 1, 0),
    "default": ("default", 0, 1, 0),
    "p1": ("pair", 5, 1, 0),
    "p2": ("pair", 5, 1, 0),
    "hi": ("tier", 10, 1, 0),
    "lo": ("tier", 0, 1, 0),
    "off": ("tier", 20, 2, 0),
    "z1": ("flat", 0, 1, 100),
    "z2": ("flat", 0, 1, 0),
    "shared": ("alpha,beta", 0, 1, 0),
    "dbl": ("double", 0, 1, 0),
    "n0": ("negtier", 0, 1, 0),
    "n5": ("negtier", -5, 1, 0),
    "multi": ("multi,multi3", 0, 1, 0),
}
# Channels serving more than MODEL: name -> comma-separated models (custom-model is not a built-in model name).
MULTI_MODELS = ("gpt-3.5-turbo,gpt-4,text-davinci-003,text-embedding-ada-002,text-moderation-latest,"
                "dall-e-2,dall-e-3,tts-1,custom-model")
CHANNEL_MODELS = {"multi": MULTI_MODELS}
LIMITED_KV_DB = 5
API_LIMIT = 25  # GLOBAL_API_RATE_LIMIT of the second, rate-limited instance


def pytest_collection_modifyitems(items):
    # Session setup (two AWS engines, program start, channel sync) is charged to the first test run.
    if items:
        items[0].add_marker(pytest.mark.timeout(600))


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- stub upstreams
class Stubs:
    """OpenAI-compatible upstreams answering per endpoint. A request body may carry `#usage:P/C#` or `#status:NNN#` directives.

    Every reply that the real API prices by tokens carries `usage`; streamed chat ends with a usage chunk.
    """

    def __init__(self, names):
        self.calls = []
        self.lock = threading.Lock()
        self.servers = {}
        self.urls = {}
        for name in names:
            server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler(name))
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.servers[name] = server
            self.urls[name] = f"http://127.0.0.1:{server.server_address[1]}"

    @staticmethod
    def reply(name, model, usage):
        return {
            "id": f"chatcmpl-{name}",
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": f"reply from {name}"}}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]},
        }

    @staticmethod
    def text_reply(name, model, usage):
        return {
            "id": f"cmpl-{name}",
            "object": "text_completion",
            "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "text": f"text from {name}", "finish_reason": "stop"}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]},
        }

    @staticmethod
    def stream_events(name, model, usage):
        chunk = {"id": f"chatcmpl-{name}", "object": "chat.completion.chunk", "created": 1700000000, "model": model}
        return [
            {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "reply "}, "finish_reason": None}]},
            {**chunk, "choices": [{"index": 0, "delta": {"content": f"from {name}"}, "finish_reason": "stop"}]},
            {**chunk, "choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]}},
        ]

    @staticmethod
    def stream(name, model, usage):
        events = Stubs.stream_events(name, model, usage)
        return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"

    @staticmethod
    def audio(name):
        return b"ID3" + f"speech from {name}".encode()

    def _handler(self, name):
        stubs = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _body(self):
                if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                    raw = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            return raw
                        raw += self.rfile.read(size)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                raw = self._body()
                try:
                    request = json.loads(raw or b"{}")
                except ValueError:
                    request = {"unparsed": raw.decode(errors="replace")}
                text = json.dumps(request)
                usage, status = (100, 50), 200
                if "#usage:" in text:
                    prompt, completion = text.split("#usage:")[1].split("#")[0].split("/")
                    usage = (int(prompt), int(completion))
                if "#status:" in text:
                    status = int(text.split("#status:")[1][:3])
                with stubs.lock:
                    stubs.calls.append({"upstream": name, "path": self.path, "auth": self.headers.get("Authorization"), "body": request, "text": text})
                path, content_type = self.path.split("?")[0], "application/json"
                if status != 200:
                    obj = {"error": {"message": "upstream failure", "type": "server_error", "code": "boom"}}
                elif path.endswith("/chat/completions") and request.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(Stubs.stream(name, request.get("model"), usage))
                    self.close_connection = True
                    return
                elif path.endswith("/chat/completions"):
                    obj = Stubs.reply(name, request.get("model"), usage)
                elif path.endswith("/completions"):
                    obj = Stubs.text_reply(name, request.get("model"), usage)
                elif path.endswith("/embeddings"):
                    obj = {"object": "list", "model": request.get("model"),
                           "data": [{"object": "embedding", "index": 0, "embedding": [0.5, -0.25]}],
                           "usage": {"prompt_tokens": usage[0], "total_tokens": usage[0]}}
                elif path.endswith("/moderations"):
                    obj = {"id": f"modr-{name}", "model": request.get("model"), "results": [{"flagged": False}],
                           "usage": {"prompt_tokens": usage[0], "total_tokens": usage[0]}}
                elif path.endswith("/images/generations"):
                    obj = {"created": 1700000000, "data": [{"url": f"https://images.invalid/{name}.png"}]}
                elif path.endswith("/audio/speech"):
                    obj, content_type = None, "audio/mpeg"
                else:
                    obj = {"error": {"message": f"stub has no {path}", "type": "invalid_request_error"}}
                    status = 404
                body = Stubs.audio(name) if obj is None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def tagged(self, tag):
        with self.lock:
            return [call for call in self.calls if tag in call["text"]]

    def close(self):
        for server in self.servers.values():
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------- AWS engines
def _wait_reachable(db, kv, deadline):
    dsn = f"postgresql://relayadmin:relayPass123@{db['Address']}:{db['Port']}/postgres"
    pg_ok = kv_ok = 0
    last = None
    # Require several consecutive successes so a forwarder that is just appearing is really up.
    while pg_ok < 3 or kv_ok < 3:
        if time.time() > deadline:
            raise AssertionError(f"engines not reachable at reported endpoints: {last}")
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                conn.execute("SELECT 1")
            pg_ok += 1
        except psycopg.Error as error:
            pg_ok, last = 0, error
        for host in {kv["Address"], "127.0.0.1" if kv["Address"] == "localhost" else kv["Address"]}:
            try:
                client = redis.Redis(host=host, port=kv["Port"], socket_connect_timeout=3, socket_timeout=3)
                if not client.ping():
                    raise redis.RedisError("no PONG")
                client.close()
            except redis.RedisError as error:
                kv_ok, last = -1, error
                break
        kv_ok += 1
        time.sleep(1)


# ---------------------------------------------------------------- AWS engines
@pytest.fixture(scope="session")
def engines():
    ident = "cmr" + uuid.uuid4().hex[:10]
    rds, cache = boto3.client("rds"), boto3.client("elasticache")
    db_id, kv_id = f"{ident}-db", f"{ident}-kv"
    created = []
    try:
        rds.create_db_instance(DBInstanceIdentifier=db_id, Engine="postgres", DBInstanceClass="db.t3.micro",
                               MasterUsername="relayadmin", MasterUserPassword="relayPass123", AllocatedStorage=20)
        created.append("db")
        cache.create_replication_group(ReplicationGroupId=kv_id, ReplicationGroupDescription="relay cache",
                                       Engine="valkey", CacheNodeType="cache.t3.micro", NumCacheClusters=1)
        created.append("kv")
        db = kv = None
        deadline = time.time() + 400
        while db is None or kv is None:
            assert time.time() < deadline, "AWS engines did not become available"
            if db is None:
                instance = rds.describe_db_instances(DBInstanceIdentifier=db_id)["DBInstances"][0]
                if instance.get("DBInstanceStatus") == "available" and instance.get("Endpoint"):
                    db = instance["Endpoint"]
            if kv is None:
                group = cache.describe_replication_groups(ReplicationGroupId=kv_id)["ReplicationGroups"][0]
                if group.get("Status") == "available":
                    kv = group["NodeGroups"][0]["PrimaryEndpoint"]
            if db is None or kv is None:
                time.sleep(2)
        # "available" can precede reachability: the venue forwards endpoints reported as localhost after polling
        # AWS, and the engines themselves may still be starting. Wait until both answer at the reported endpoints.
        _wait_reachable(db, kv, deadline=time.time() + 180)
        yield {"db": db, "kv": kv, "user": "relayadmin", "password": "relayPass123"}
    finally:
        if "db" in created:
            try:
                rds.delete_db_instance(DBInstanceIdentifier=db_id, SkipFinalSnapshot=True)
            except Exception as exc:  # noqa: BLE001 -- cleanup must not mask results
                print("cleanup rds:", exc)
        if "kv" in created:
            try:
                cache.delete_replication_group(ReplicationGroupId=kv_id)
            except Exception as exc:  # noqa: BLE001
                print("cleanup elasticache:", exc)


# ---------------------------------------------------------------- the server under test
class Relay:
    def __init__(self, engines, root, stubs, kv_db=KV_DB, env=None):
        db, kv = engines["db"], engines["kv"]
        self.root = root
        self.private = tempfile.mkdtemp(prefix="relay-output-")  # root-private; never inside the candidate's tree
        self.stubs = stubs
        (root / "logs").mkdir()
        give_to_candidate(root / "logs")
        self.dbname = "relay_" + uuid.uuid4().hex[:8]
        admin_dsn = f"postgresql://{engines['user']}:{engines['password']}@{db['Address']}:{db['Port']}/postgres"
        deadline = time.time() + 120
        while True:
            try:
                with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as conn:
                    conn.execute(f"CREATE DATABASE {self.dbname}")
                break
            except psycopg.OperationalError:
                if time.time() > deadline:
                    raise
                time.sleep(1)
        self.dsn = f"postgres://{engines['user']}:{engines['password']}@{db['Address']}:{db['Port']}/{self.dbname}?sslmode=disable"
        self.kv = redis.Redis(host=kv["Address"], port=kv["Port"], db=kv_db, decode_responses=True)
        self.env = {
            "SQL_DSN": self.dsn,
            "REDIS_CONN_STRING": f"redis://{kv['Address']}:{kv['Port']}/{kv_db}",
            "SYNC_FREQUENCY": str(SYNC),
            "SESSION_SECRET": "relay-session-" + secrets.token_hex(8),
            "GLOBAL_API_RATE_LIMIT": "1000000",
            "TIKTOKEN_CACHE_DIR": "/app/tiktoken",
            **(env or {}),
        }
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self.output = None
        self.starts = 0

    def setup(self):
        for attempt in range(3):
            try:
                self.start()
                break
            except AssertionError:
                if attempt == 2 or self.proc.poll() is None:
                    raise
                self.output.close()
                time.sleep(3)  # an engine endpoint may only just have become reachable
        self.sql = psycopg.connect(self.dsn, autocommit=True)
        self.channel_ids = {}
        for name, (groups, priority, status, weight) in CHANNELS.items():
            self.channel_ids[name] = self._channel(name, self.stubs.urls[name], groups, priority, status, weight)
        self.q(
            "INSERT INTO options(key, value) VALUES ('GroupRatio', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            json.dumps(GROUP_RATIO),
        )
        time.sleep(SYNC + 2)  # channel and option rows are re-read every SYNC_FREQUENCY seconds

    # -- process lifecycle
    def start(self):
        self.starts += 1
        self.output = open(os.path.join(self.private, f"server-{self.starts}.out"), "wb")  # noqa: SIM115 -- closed in stop()
        self.proc = start_command(["/app/program", "--port", str(self.port), "--log-dir", str(self.root / "logs")], cwd=self.root,
                                  env=self.env, stdout=self.output, stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        while True:
            try:
                with urllib.request.urlopen(self.base + "/api/status", timeout=3) as response:
                    if json.loads(response.read()).get("success") is True:
                        return
            except (OSError, ValueError):
                pass
            if self.proc.poll() is not None:
                raise AssertionError(f"/app/program exited with {self.proc.returncode} before answering /api/status")
            if time.time() > deadline:
                raise AssertionError("/app/program did not answer GET /api/status")
            time.sleep(0.5)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        if self.output is not None:
            self.output.close()

    def ensure_running(self):
        if self.proc.poll() is not None:
            self.output.close()
            self.start()

    # -- SQL fixtures and inspection
    def q(self, statement, *args):
        cursor = self.sql.execute(statement, args)
        return cursor.fetchall() if cursor.description else None

    def rows(self, statement, *args):
        cursor = self.sql.execute(statement, args)
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _channel(self, name, base_url, groups, priority, status, weight):
        channel_id = self.q(
            'INSERT INTO channels(type, key, status, name, base_url, models, "group", priority, weight, created_time) '
            "VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            f"sk-upstream-{name}", status, f"ch-{name}", base_url, CHANNEL_MODELS.get(name, MODEL), groups, priority, weight, int(time.time()),
        )[0][0]
        for group in groups.split(","):
            for model in CHANNEL_MODELS.get(name, MODEL).split(","):
                self.q('INSERT INTO abilities("group", model, channel_id, enabled, priority) VALUES (%s, %s, %s, %s, %s)',
                       group, model, channel_id, status == 1, priority)
        return channel_id

    def user(self, group="vip", quota=1_000_000, status=1, role=1):
        name = "u" + secrets.token_hex(5)
        access = secrets.token_hex(16)
        user_id = self.q(
            'INSERT INTO users(username, password, display_name, role, status, access_token, quota, "group", aff_code) '
            "VALUES (%s, 'not-a-login-password', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            name, name, role, status, access, quota, group, secrets.token_hex(8),
        )[0][0]
        return {"id": user_id, "access": access, "group": group, "username": name}

    def user_row(self, user_id):
        return self.rows("SELECT quota, used_quota, request_count, status FROM users WHERE id = %s", user_id)[0]

    def token_row(self, token_id):
        found = self.rows(
            "SELECT id, user_id, key, status, name, created_time, expired_time, remain_quota, unlimited_quota, used_quota, models, subnet "
            "FROM tokens WHERE id = %s", token_id)
        return found[0] if found else None

    def tokens_of(self, user_id):
        return self.rows("SELECT id, name, status, remain_quota, used_quota FROM tokens WHERE user_id = %s ORDER BY id", user_id)

    def wait_for(self, predicate, timeout=10.0):
        """Poll until predicate() is truthy (quota writes may land shortly after the response)."""
        deadline = time.time() + timeout
        while True:
            value = predicate()
            if value or time.time() > deadline:
                return value
            time.sleep(0.2)

    # -- HTTP
    def call(self, method, path, body=None, auth=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/json"}
        if auth is not None:
            headers["Authorization"] = auth
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            payload = error.read()
            status = error.code
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, payload.decode(errors="replace")

    def api(self, user, method, path, body=None, raw=None):
        return self.call(method, path, body, auth=user["access"], raw=raw)

    def create_token(self, user, **fields):
        body = {"name": "key", "remain_quota": 500_000, "expired_time": -1, "unlimited_quota": False, **fields}
        status, reply = self.api(user, "POST", "/api/token/", body)
        assert status == 200 and isinstance(reply, dict) and reply.get("success") is True, (status, reply)
        return reply["data"]

    def chat(self, key, content, model=MODEL, auth=None, extra=None):
        body = {"model": model, "messages": [{"role": "user", "content": content}], **(extra or {})}
        return self.call("POST", "/v1/chat/completions", body, auth=auth if auth is not None else f"Bearer sk-{key}")

    def logs(self, user, page=0):
        status, reply = self.api(user, "GET", f"/api/log/self?p={page}")
        assert status == 200 and reply["success"] is True, (status, reply)
        return reply["data"]


@pytest.fixture(scope="session")
def stubs():
    s = Stubs(CHANNELS)
    yield s
    s.close()


@pytest.fixture(scope="session")
def _relay(engines, stubs):
    with workspace() as root:
        server = Relay(engines, root, stubs)
        try:
            server.setup()
            yield server
        finally:
            server.stop()
            for name in sorted(os.listdir(server.private)):
                with open(os.path.join(server.private, name), "rb") as stream:
                    tail = stream.read()[-3000:].decode(errors="replace")
                print(f"---- {name} (tail)\n{tail}")
            shutil.rmtree(server.private, ignore_errors=True)


@pytest.fixture
def relay(_relay):
    _relay.ensure_running()
    return _relay


@pytest.fixture(scope="session")
def _limited(engines, stubs):
    """A second server with its own database, Valkey database LIMITED_KV_DB and GLOBAL_API_RATE_LIMIT=API_LIMIT."""
    with workspace() as root:
        server = Relay(engines, root, stubs, kv_db=LIMITED_KV_DB, env={"GLOBAL_API_RATE_LIMIT": str(API_LIMIT)})
        try:
            server.setup()
            yield server
        finally:
            server.stop()
            shutil.rmtree(server.private, ignore_errors=True)


@pytest.fixture
def limited(_limited):
    """The rate-limited server with its Valkey database emptied, so each test starts with no request history."""
    _limited.ensure_running()
    _limited.kv.flushdb()
    return _limited


@pytest.fixture
def tag():
    return "tag-" + uuid.uuid4().hex[:12]
