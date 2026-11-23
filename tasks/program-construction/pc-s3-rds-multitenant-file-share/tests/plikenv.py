"""Shared helpers: a per-test program environment (database, bucket, TOML, server) and a small HTTP client.

Everything here drives the public executable: users and tokens are provisioned with its CLI, behaviour is
observed over HTTP, and state is read independently from Postgres (psycopg) and S3 (boto3).
"""

import base64
import hashlib
import http.cookiejar
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

import boto3
import psycopg
from botocore.config import Config

from harness import run_program, start_command, write_fixture

PREFIX = "tenants"


def s3_client():
    return boto3.client("s3", config=Config(s3={"addressing_style": "path"}))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Response:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)

    def __repr__(self):
        return f"<{self.status} {self.body[:300]!r}>"


class Client:
    """One HTTP caller: optional session cookies (after login) and/or an API token header."""

    def __init__(self, base, token=None):
        self.base = base
        self.token = token
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name):
        return next((c.value for c in self.jar if c.name == name), None)

    def request(self, method, path, body=None, headers=None, ctype=None, xsrf=True):
        hdrs = dict(headers or {})
        if self.token:
            hdrs.setdefault("X-PlikToken", self.token)
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            ctype = ctype or "application/json"
        if ctype:
            hdrs["Content-Type"] = ctype
        xsrf_value = self.cookie("plik-xsrf")
        if xsrf and xsrf_value and method not in ("GET", "HEAD"):
            hdrs.setdefault("X-XSRFToken", xsrf_value)
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return Response(resp.status, resp.read(), resp.headers)
        except urllib.error.HTTPError as err:
            return Response(err.code, err.read(), err.headers)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def add_file(self, upload_id, name, data, suffix="", headers=None):
        boundary = "----t" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self.post(f"/file/{upload_id}{suffix}", body, headers=headers, ctype=f"multipart/form-data; boundary={boundary}")

    def login(self, login, password):
        return self.post("/auth/local/login", {"login": login, "password": password})


def basic(login, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()}


def md5(data):
    return hashlib.md5(data).hexdigest()


class Tenant:
    """A provisioned user with a logged-in session client and one API token client."""

    def __init__(self, env, login, password):
        self.env = env
        self.login = login
        self.password = password
        self.id = f"local:{login}"
        self.session = env.client()
        resp = self.session.login(login, password)
        assert resp.status == 200, f"login {login}: {resp}"
        resp = self.session.post("/me/token", {"comment": f"{login} main"})
        assert resp.status == 200, f"token for {login}: {resp}"
        self.token = resp.json()["token"]
        self.api = env.client(self.token)

    def new_token(self, comment=None):
        resp = self.session.post("/me/token", {"comment": comment} if comment else None)
        assert resp.status == 200, resp
        return resp.json()["token"]

    def create_upload(self, client=None, **options):
        resp = (client or self.api).post("/upload", options)
        assert resp.status == 200, f"create upload for {self.login}: {resp}"
        return resp.json()

    def upload(self, name, data, client=None, **options):
        """Create an upload holding one file; returns (upload JSON, file JSON)."""
        up = self.create_upload(client, **options)
        resp = (client or self.api).add_file(up["id"], name, data)
        assert resp.status == 200, f"add file for {self.login}: {resp}"
        return up, resp.json()


class PlikEnv:
    """A fresh database + bucket + workspace + configuration for one test; the server starts on demand."""

    def __init__(self, engine, workspace_dir):
        self.engine = engine
        self.dir = workspace_dir
        self.dbname = "t_" + uuid.uuid4().hex[:12]
        self.bucket = "plik-" + uuid.uuid4().hex[:16]
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.config = self.dir / "plik.toml"
        self.server = None
        self.log = None
        self.s3 = s3_client()
        with psycopg.connect(engine["dsn"] + " dbname=postgres", autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{self.dbname}"')
        self.s3.create_bucket(Bucket=self.bucket)
        self.write_config()

    @property
    def dsn(self):
        return f"{self.engine['dsn']} dbname={self.dbname}"

    def write_config(self, max_user_size="unlimited", port=None, extra="", data_extra="", prefix=PREFIX):
        endpoint = urlparse(os.environ.get("AWS_ENDPOINT_URL", "http://engines:4566"))
        text = f"""ListenAddress = "127.0.0.1"
ListenPort = {port or self.port}
NoWebInterface = true
FeatureAuthentication = "forced"
FeatureLocalLogin = "enabled"
FeatureApiTokens = "enabled"
FeatureRemovable = "enabled"
FeaturePassword = "enabled"
MaxUserSizeStr = "{max_user_size}"
{extra}
DataBackend = "s3"
[DataBackendConfig]
    Endpoint = "{endpoint.hostname}:{endpoint.port or 80}"
    AccessKeyID = "test"
    SecretAccessKey = "test"
    Bucket = "{self.bucket}"
    Location = "us-east-1"
    Prefix = "{prefix}"
    UseSSL = false
    BucketLookup = "path"
{data_extra}

[MetadataBackendConfig]
    Driver = "postgres"
    ConnectionString = "{self.dsn}"
"""
        write_fixture(self.config, text, mode=0o644)

    # --- program CLI -------------------------------------------------------------------------------------------
    def cli(self, *args, env=None, stdin=None):
        return run_program(["--config", self.config, *args], cwd=self.dir, env=env, stdin=stdin, timeout=45)

    def add_user(self, login, password, max_user_size=None):
        args = ["user", "create", "--login", login, "--password", password]
        if max_user_size is not None:
            args += ["--max-user-size", max_user_size]
        result = self.cli(*args)
        assert result.returncode == 0, f"user create {login}: {result.stdout}{result.stderr}"
        return result

    # --- server ------------------------------------------------------------------------------------------------
    def start(self, extra_args=(), port=None):
        port = port or self.port
        self.log = tempfile.TemporaryFile()
        self.server = start_command(
            ["/app/program", "--config", str(self.config), *map(str, extra_args)],
            cwd=self.dir,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.server.poll() is not None:
                raise AssertionError(f"server exited with {self.server.returncode}: {self.server_log()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                    return self
            except Exception:
                time.sleep(0.25)
        raise AssertionError(f"server did not become healthy on port {port}: {self.server_log()}")

    def server_log(self):
        if self.log is None:
            return ""
        self.log.seek(0)
        return self.log.read()[-4000:].decode("utf-8", "replace")

    def stop(self):
        if self.server is not None:
            self.server.terminate()
            try:
                self.server.wait(10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(10)
            self.server = None

    def client(self, token=None):
        return Client(self.base, token)

    def tenant(self, login, password):
        return Tenant(self, login, password)

    # --- independent state observation ---------------------------------------------------------------------------
    def sql(self, query, *args):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return conn.execute(query, args).fetchall()

    def objects(self):
        """Every object in the bucket: key -> bytes."""
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            for item in page.get("Contents", []):
                out[item["Key"]] = self.s3.get_object(Bucket=self.bucket, Key=item["Key"])["Body"].read()
        return out

    def key(self, upload_id, file_id):
        return f"{PREFIX}/{upload_id}.{file_id}"

    def owner_state(self, user_id):
        """Uploads rows, files rows and S3 objects belonging to one user's uploads."""
        uploads = self.sql(
            'SELECT id, "user", token, upload_token, removable, deleted_at FROM uploads WHERE "user" = %s ORDER BY id', user_id
        )
        files = self.sql(
            'SELECT f.id, f.upload_id, f.name, f.size, f.status FROM files f JOIN uploads u ON u.id = f.upload_id '
            'WHERE u."user" = %s ORDER BY f.id',
            user_id,
        )
        ids = {row[0] for row in uploads}
        objects = {k: v for k, v in self.objects().items() if k.split("/", 1)[-1].split(".", 1)[0] in ids}
        return uploads, files, objects

    def close(self):
        try:
            self.stop()
        finally:
            if self.log is not None:
                self.log.close()
            try:
                for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
                    for item in page.get("Contents", []):
                        self.s3.delete_object(Bucket=self.bucket, Key=item["Key"])
                self.s3.delete_bucket(Bucket=self.bucket)
            except Exception:
                pass
            try:
                with psycopg.connect(self.engine["dsn"] + " dbname=postgres", autocommit=True) as conn:
                    conn.execute(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')
            except Exception:
                pass


# --- assertion helpers -------------------------------------------------------------------------------------------
def error(resp, status, message):
    """A plain-text error: exact status and message, trailing whitespace ignored."""
    assert resp.status == status, resp
    assert resp.text.rstrip() == message, resp


def ok(resp):
    assert resp.status == 200, resp
    assert resp.text.rstrip() == "ok", resp


def boolean(value, expected):
    assert type(value) is bool and value is expected, f"expected JSON boolean {expected}, got {value!r}"


def number(value, expected):
    """Numeric value equivalence (1, 1.0, 1e0 alike); booleans excluded."""
    assert type(value) in (int, float) and value == expected, f"expected number {expected}, got {value!r}"


def empty(value):
    """An absent, null or empty value (strings and lists)."""
    assert value in (None, "", []), f"expected empty, got {value!r}"


def check_file(obj, name, data, status="uploaded", file_type="text/plain; charset=utf-8"):
    assert obj["fileName"] == name, obj
    assert obj["status"] == status, obj
    number(obj["fileSize"], len(data))
    assert obj["fileMd5"] == md5(data), obj
    assert obj["fileType"] == file_type, obj
    assert isinstance(obj["id"], str) and obj["id"], obj


def check_upload_flags(obj, *, removable=False, one_shot=False, protected=False, admin=None):
    boolean(obj["removable"], removable)
    boolean(obj["oneShot"], one_shot)
    boolean(obj["protectedByPassword"], protected)
    if admin is not None:
        boolean(obj["admin"], admin)


def page(resp):
    """A paginated listing response; returns its decoded body after checking its results list.

    Cursors are opaque; an absent, null or empty cursor means no further page."""
    assert resp.status == 200, resp
    body = resp.json()
    assert isinstance(body["results"], list), body
    return body


def counts(obj, expected):
    """A JSON object of numeric counters, compared by numeric value with booleans excluded."""
    assert set(obj) == set(expected), obj
    for key, value in expected.items():
        number(obj[key], value)


def solo(plik, login="alice", password="alice-secret-1", limit="-1", **config):
    """Write the configuration (write_config keyword arguments), provision one user, start the server, log in."""
    if config:
        plik.write_config(**config)
    plik.add_user(login, password, limit)
    plik.start()
    return plik.tenant(login, password)


def wait_until(predicate, timeout=30, interval=0.2):
    """Poll until predicate() is truthy; returns its last value."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() > deadline:
            return value
        time.sleep(interval)
