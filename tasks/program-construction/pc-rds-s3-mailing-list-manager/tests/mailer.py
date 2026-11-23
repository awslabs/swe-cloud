"""Shared verifier helpers: run the candidate's server against a fresh database and talk to its API."""

import base64
import io
import json
import os
import signal
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

import psycopg

from harness import run_program, start_command, write_fixture

ENDPOINT = "http://engines:4566"
ADMIN_ENV = {"LISTMONK_ADMIN_USER": "admin", "LISTMONK_ADMIN_PASSWORD": "adminpass-1", "LISTMONK_ADMIN_API_USER": "apibot"}
TOKEN_PREFIX = "export LISTMONK_ADMIN_API_TOKEN="


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def config_text(engine, dbname, port):
    return (
        f'[app]\naddress = "127.0.0.1:{port}"\n\n'
        f'[db]\nhost = "{engine.host}"\nport = {engine.port}\nuser = "{engine.user}"\npassword = "{engine.password}"\n'
        f'database = "{dbname}"\nssl_mode = "disable"\nmax_open = 10\nmax_idle = 10\nmax_lifetime = "300s"\nparams = ""\n'
    )


def parse_token(stderr):
    lines = [line.strip() for line in stderr.splitlines() if line.strip().startswith(TOKEN_PREFIX)]
    assert len(lines) == 1, f"expected one API token line on stderr, got: {stderr[-2000:]!r}"
    value = lines[0][len(TOKEN_PREFIX):]
    assert len(value) >= 3 and value[0] == value[-1] == '"', lines[0]
    return value[1:-1]


def multipart(fields=None, files=None):
    boundary = "----verifier" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in (fields or {}).items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, ctype, data) in (files or {}).items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), "multipart/form-data; boundary=" + boundary


class Database:
    """A fresh, empty Postgres database on the shared engine plus a candidate workspace holding config.toml."""

    def __init__(self, engine, root):
        self.engine = engine
        self.name = engine.create_database()
        self.root = root
        self.port = free_port()
        write_fixture(root / "config.toml", config_text(engine, self.name, self.port), mode=0o644)
        self.conn = psycopg.connect(host=engine.host, port=engine.port, user=engine.user, password=engine.password, dbname=self.name, autocommit=True)

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def tables(self):
        return {row[0] for row in self.q("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}

    def run(self, *args, env=None, timeout=60):
        return run_program([*args], cwd=self.root, env=env, timeout=timeout)

    def install(self, *extra, env=ADMIN_ENV):
        result = self.run("--install", "--yes", "--config", "config.toml", *extra, env=env)
        assert result.returncode == 0, f"install failed ({result.returncode}): {result.stdout[-1500:]} {result.stderr[-1500:]}"
        return result

    def close(self):
        self.conn.close()
        self.engine.drop_database(self.name)


class App:
    """The candidate's HTTP server running on one installed database."""

    def __init__(self, database, token):
        self.db = database
        self.token = token
        self.base = f"http://127.0.0.1:{database.port}"
        self.auth = "Basic " + base64.b64encode(f"apibot:{token}".encode()).decode()
        self.log = tempfile.TemporaryFile()
        self.proc = start_command(["/app/program", "--config", "config.toml"], cwd=database.root, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            self.wait_healthy()
        except BaseException:
            self.stop()
            raise

    def log_tail(self, size=3000):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")[-size:]

    def wait_healthy(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            assert self.proc.poll() is None, f"server exited with {self.proc.returncode}: {self.log_tail()}"
            try:
                status, body = self.call("GET", "/api/health", retry=False)
                if status == 200 and body == {"data": True}:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError(f"server never became healthy: {self.log_tail()}")

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(10)
        self.log.close()

    def raw(self, method, path, body=None, *, ctype="application/json", auth=True, retry=True):
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        deadline = time.time() + (10 if retry else 0)
        while True:
            request = urllib.request.Request(self.base + path, data=data, method=method)
            if auth is True:
                request.add_header("Authorization", self.auth)
            elif auth:
                request.add_header("Authorization", auth)
            if data is not None:
                request.add_header("Content-Type", ctype)
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
            except urllib.error.URLError as error:
                # A refused connection was never processed; it happens while the server reloads.
                if isinstance(error.reason, ConnectionRefusedError) and time.time() < deadline:
                    time.sleep(0.3)
                    continue
                raise

    def call(self, method, path, body=None, **options):
        status, payload = self.raw(method, path, body, **options)
        try:
            return status, json.loads(payload, parse_float=value_number) if payload else None
        except ValueError:
            return status, payload

    def ok(self, method, path, body=None, **options):
        status, payload = self.call(method, path, body, **options)
        assert status == 200, (method, path, status, payload)
        assert isinstance(payload, dict) and set(payload) == {"data"}, payload
        return payload["data"]

    def get(self, path):
        return self.ok("GET", path)

    def settings(self):
        return self.get("/api/settings")

    def update_settings(self, change):
        """GET the settings, apply change(settings) in place, PUT them back and wait for the reload."""
        settings = self.settings()
        change(settings)
        status, payload = self.call("PUT", "/api/settings", settings)
        if status == 200:
            time.sleep(2.0)
            self.wait_healthy()
        return status, payload

    def configure(self, change):
        status, payload = self.update_settings(change)
        assert status == 200, (status, payload)

    def q(self, sql, *args):
        return self.db.q(sql, *args)

    # Domain helpers.
    def create_list(self, name, type_="private", optin="single", **extra):
        return self.ok("POST", "/api/lists", {"name": name, "type": type_, "optin": optin, **extra})

    def create_subscriber(self, email, **fields):
        return self.ok("POST", "/api/subscribers", {"email": email, **fields})

    def query_subscribers(self, **params):
        return self.get("/api/subscribers?" + urllib.parse.urlencode({"per_page": "all", **params}, doseq=True))

    def start_import(self, content, params, filename="subscribers.csv", ctype="text/csv"):
        data = content.encode() if isinstance(content, str) else content
        body, multipart_type = multipart({"params": json.dumps(params)}, {"file": (filename, ctype, data)})
        return self.call("POST", "/api/import/subscribers", body, ctype=multipart_type)

    def wait_import(self, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.get("/api/import/subscribers")
            if state["status"] not in ("importing", "stopping"):
                return state
            time.sleep(0.2)
        raise AssertionError(f"import did not finish: {state}")

    def run_import(self, content, params, **options):
        status, payload = self.start_import(content, params, **options)
        assert status == 200, (status, payload)
        return self.wait_import()

    def upload(self, filename, ctype, data):
        body, multipart_type = multipart(files={"file": (filename, ctype, data)})
        return self.call("POST", "/api/media", body, ctype=multipart_type)


def start_app(database):
    """Install the database and start serving it."""
    result = database.install()
    return App(database, parse_token(result.stderr))


def quiet_smtp(settings):
    """No mail server exists: point every SMTP server at a closed local port so mail attempts fail fast."""
    for server in settings["smtp"]:
        server.update(host="127.0.0.1", port=1)


# Value assertions shared by the modules.
def value_number(text):
    """Numbers compare by value: an integral 3.0 or 3e0 decodes like 3, so it also works as an id in later requests."""
    number = float(text)
    return int(number) if number.is_integer() else number


def assert_id(value):
    assert type(value) in (int, float) and value == int(value) and value > 0, value


IMPORT_FIELDS = ("name", "total", "imported", "status")


def assert_import(observed, expected):
    """Compare the documented import-status fields; other fields may be present."""
    assert isinstance(observed, dict) and set(IMPORT_FIELDS) <= set(observed), observed
    assert_json({key: observed[key] for key in IMPORT_FIELDS}, expected)


def is_number(value, expected):
    return type(value) in (int, float) and value == expected


def same_json(observed, expected):
    """Decoded-JSON equality that keeps booleans distinct from numbers (numbers compare by value)."""
    if isinstance(expected, bool) or expected is None:
        return observed is expected
    if isinstance(expected, (int, float)):
        return is_number(observed, expected)
    if isinstance(expected, str):
        return isinstance(observed, str) and observed == expected
    if isinstance(expected, list):
        return isinstance(observed, list) and len(observed) == len(expected) and all(same_json(o, e) for o, e in zip(observed, expected))
    if isinstance(expected, dict):
        return isinstance(observed, dict) and set(observed) == set(expected) and all(same_json(observed[k], v) for k, v in expected.items())
    raise TypeError(expected)


def assert_json(observed, expected):
    assert same_json(observed, expected), f"{observed!r} != {expected!r}"


def png_bytes(width, height, rgb=(200, 30, 30)):
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR", data[:16]
    return struct.unpack(">II", data[16:24])
