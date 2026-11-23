"""Test-side helpers: workspace/server lifecycle, HTTP calls, SQL inspection and small assertions."""

import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from harness import run_command, start_command, workspace, write_fixture

TENANTS = {
    "acme": {"app": "app-acme", "client_id": "acme-7f3a1c", "secret": "acme-secret-91c2d"},
    "globex": {"app": "app-globex", "client_id": "globex-2b8e44", "secret": "globex-secret-0d5f7"},
}

# Profile fields admitted by the public interface (JSON name -> column name).
PROFILE_FIELDS = {
    "displayName": "display_name",
    "firstName": "first_name",
    "lastName": "last_name",
    "email": "email",
    "title": "title",
    "bio": "bio",
    "affiliation": "affiliation",
    "tag": "tag",
    "homepage": "homepage",
    "location": "location",
    "region": "region",
    "language": "language",
    "gender": "gender",
    "birthday": "birthday",
    "education": "education",
}

SEED_USERS = {
    ("acme", "dana"): {"displayName": "Dana Acme", "email": "dana@acme.test", "title": "Analyst", "bio": "acme-dana-bio", "affiliation": "Acme Labs"},
    ("acme", "erin"): {"displayName": "Erin", "firstName": "Erin", "lastName": "Ng", "email": "erin@acme.test"},
    ("globex", "dana"): {"displayName": "Dana Globex", "email": "dana@globex.test", "title": "Director", "bio": "globex-dana-bio"},
    ("globex", "gus"): {"displayName": "Gus", "tag": "ops"},
}
SEED_PASSWORDS = {("acme", "dana"): "Dana-pass-1"}


def init_data(orgs=("acme", "globex"), users=SEED_USERS, passwords=SEED_PASSWORDS, tenants=TENANTS):
    organizations, applications, records = [], [], []
    for org in orgs:
        organizations.append(
            {"owner": "admin", "name": org, "displayName": org.title(), "passwordType": "bcrypt", "isProfilePublic": False,
             "countryCodes": ["US"], "languages": ["en"], "initScore": 0}
        )
        t = tenants[org]
        applications.append(
            {"owner": "admin", "name": t["app"], "displayName": "App " + org, "organization": org, "clientId": t["client_id"],
             "clientSecret": t["secret"], "enablePassword": True, "redirectUris": [], "grantTypes": ["client_credentials"],
             "tokenFormat": "JWT", "expireInHours": 1}
        )
    for (owner, name), fields in users.items():
        record = {"owner": owner, "name": name, "type": "normal-user", **fields}
        if (owner, name) in passwords:
            record["password"] = passwords[(owner, name)]
        records.append(record)
    return {"organizations": organizations, "applications": applications, "users": records}


TENANT_INIT = init_data()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def admin_connect(rds, dbname="postgres"):
    return psycopg.connect(host=rds["host"], port=rds["port"], user=rds["user"], password=rds["password"], dbname=dbname,
                           autocommit=True, connect_timeout=10)


class Response:
    def __init__(self, code, raw):
        self.code = code
        self.raw = raw
        try:
            self.json = json.loads(raw)
        except ValueError:
            pytest.fail(f"response is not JSON (HTTP {code}): {raw[:300]!r}")

    def __repr__(self):
        return f"<HTTP {self.code} {self.raw[:400]!r}>"


class Directory:
    """One workspace, one database name and (when started) one candidate server process."""

    def __init__(self, rds):
        self.rds = rds
        self.db = "dir_" + uuid.uuid4().hex[:12]
        self.proc = None
        self.log = None

    def __enter__(self):
        self._ws = workspace()
        self.root = self._ws.__enter__()
        self.port = free_port()
        return self

    def __exit__(self, *exc):
        try:
            self.stop()
        finally:
            try:
                with admin_connect(self.rds) as c:
                    c.execute(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)')
            finally:
                self._ws.__exit__(*exc)

    # ---- files and process -------------------------------------------------
    def config_text(self, port=None, db=None):
        port = port or self.port
        db = db or self.db
        r = self.rds
        return (
            f"httpport = {port}\nrunmode = prod\ncopyrequestbody = true\ndriverName = postgres\n"
            f"dataSourceName = user={r['user']} password={r['password']} host={r['host']} port={r['port']} sslmode=disable dbname={db}\n"
            f"dbName = {db}\norigin = http://127.0.0.1:{port}\nldapServerPort = 0\nldapsServerPort = 0\nradiusServerPort = 0\n"
            'logConfig = {"adapter":"console"}\ninitDataFile = ./init_data.json\ninitDataNewOnly = true\nlicenseServer = none\n'
        )

    def write_files(self, init, config="conf/app.conf", port=None, db=None):
        write_fixture(self.root / "init_data.json", json.dumps(init, indent=1), mode=0o644)
        if config:
            write_fixture(self.root / config, self.config_text(port, db), mode=0o644)

    def start(self, *args, port=None, ready_timeout=45):
        assert self.proc is None
        port = port or self.port
        self.log = tempfile.TemporaryFile()
        self.proc = start_command(["/app/program", *args], cwd=self.root, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                pytest.fail(f"server exited with status {code} during start-up:\n{self.log_tail()}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/get-user?id=probe/probe", timeout=2).read()
                return
            except urllib.error.HTTPError:
                return
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        pytest.fail(f"server did not answer on port {port} within {ready_timeout}s:\n{self.log_tail()}")

    def log_tail(self):
        if self.log is None:
            return ""
        self.log.seek(0)
        return self.log.read()[-3000:].decode(errors="replace")

    def stop(self):
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 10)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        try:  # stray members of the group
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run_once(self, *args, timeout=40):
        """Run the program to completion (for start-up failures)."""
        return run_command(["/app/program", *args], cwd=self.root, capture_output=True, timeout=timeout)

    # ---- HTTP ----------------------------------------------------------------
    def call(self, method, path, cred="acme", body=None, port=None):
        """cred: tenant name (HTTP Basic), ("basic", id, secret), ("query", id, secret) or None."""
        url = f"http://127.0.0.1:{port or self.port}{path}"
        headers = {}
        if isinstance(cred, str):
            t = TENANTS[cred]
            cred = ("basic", t["client_id"], t["secret"])
        if cred is not None:
            kind, cid, secret = cred
            if kind == "basic":
                headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()
            else:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"clientId": cid, "clientSecret": secret})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return Response(response.status, response.read())
        except urllib.error.HTTPError as error:
            return Response(error.code, error.read())

    def get(self, path, cred="acme"):
        return self.call("GET", path, cred)

    def post(self, path, body, cred="acme"):
        return self.call("POST", path, cred, body)

    def user(self, owner, name, cred=None):
        """The user object returned by get-user (requires success)."""
        return ok(self.get(f"/api/get-user?id={owner}/{name}", cred or owner))

    # ---- SQL ---------------------------------------------------------------
    def sql(self, query, *params):
        with admin_connect(self.rds, self.db) as c:
            cur = c.execute(query, params)
            return cur.fetchall() if cur.description else None

    def users_snapshot(self, without=None, owner=None):
        """Complete stored user rows (all columns), ordered by key; optionally excluding one user or limited to one owner."""
        if without is not None:
            return self.sql('SELECT * FROM "user" WHERE NOT (owner = %s AND name = %s) ORDER BY owner, name', *without)
        if owner is not None:
            return self.sql('SELECT * FROM "user" WHERE owner = %s ORDER BY owner, name', owner)
        return self.sql('SELECT * FROM "user" ORDER BY owner, name')

    def row(self, owner, name):
        """Admitted columns of one stored user, or None."""
        columns = ["owner", "name", "id", "created_time", "updated_time", *PROFILE_FIELDS.values()]
        rows = self.sql(f'SELECT {", ".join(columns)} FROM "user" WHERE owner = %s AND name = %s', owner, name)
        assert len(rows) <= 1
        return dict(zip(columns, rows[0])) if rows else None

    def names(self, owner):
        return sorted(r[0] for r in self.sql('SELECT name FROM "user" WHERE owner = %s', owner))


# ---- assertions --------------------------------------------------------------
def envelope(resp):
    assert resp.code == 200, resp
    assert isinstance(resp.json, dict), resp
    assert resp.json.get("status") in ("ok", "error"), resp
    return resp.json


def ok(resp, data=...):
    doc = envelope(resp)
    assert doc["status"] == "ok" and doc["msg"] == "", resp
    if data is not ...:
        assert type(doc["data"]) is type(data) and doc["data"] == data, resp
    return doc["data"]


def error(resp, msg):
    doc = envelope(resp)
    assert (doc["status"], doc["msg"]) == ("error", msg), resp
    assert doc.get("data") is None, resp


def refused(resp):
    error(resp, "Unauthorized operation")


def is_count(value, expected):
    return type(value) in (int, float) and value == expected


def assert_profile(user, owner, name, password=False, **fields):
    """Owner, name, every admitted profile field (default empty string) and the masked password."""
    assert isinstance(user, dict), user
    assert (user.get("owner"), user.get("name")) == (owner, name), user
    for field in PROFILE_FIELDS:
        expected = fields.get(field, "")
        assert type(user.get(field)) is str and user[field] == expected, (field, user.get(field), expected)
    assert user.get("password") == ("***" if password else ""), user.get("password")


def assert_row(row, owner, name, **fields):
    assert row is not None, (owner, name)
    assert (row["owner"], row["name"]) == (owner, name)
    for field, column in PROFILE_FIELDS.items():
        assert row[column] == fields.get(field, ""), (column, row[column], fields.get(field, ""))


def parse_time(value):
    assert isinstance(value, str) and value, value
    stamp = datetime.fromisoformat(value)
    assert stamp.tzinfo is not None, value
    return stamp


def assert_recent(value, since, slack=5):
    stamp = parse_time(value)
    now = datetime.now(timezone.utc)
    assert since - timedelta(seconds=slack) <= stamp <= now + timedelta(seconds=slack), (value, since, now)


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


# ---- wider scope: storage, OAuth, certificates -----------------------------------
APP_ACME_INDEX = 0  # TENANT_INIT lists acme's organization and application first


def provider_link(name):
    """An application's reference to a provider by name."""
    return {"name": name, "canSignUp": False, "canSignIn": False, "canUnlink": False, "prompted": False, "alertType": "None"}


def storage_provider(name, bucket, owner="admin", **fields):
    """An S3 storage provider object for the init-data file, pointed at the venue's AWS endpoint."""
    return {"owner": owner, "name": name, "displayName": name, "category": "Storage", "type": "AWS S3", "clientId": "test",
            "clientSecret": "test", "regionId": "us-east-1", "bucket": bucket, "endpoint": "http://engines:4566", "domain": "",
            "pathPrefix": "", **fields}


def wider_init(grant_types=None, providers=(), app_providers=(), certs=(), app_cert=None, org_fields=None, users=None):
    """TENANT_INIT with extra objects; changes apply to acme's application/organization."""
    init = json.loads(json.dumps(init_data(users=users) if users is not None else TENANT_INIT))
    app = init["applications"][APP_ACME_INDEX]
    if grant_types is not None:
        app["grantTypes"] = list(grant_types)
    if app_providers:
        app["providers"] = [provider_link(n) for n in app_providers]
    if app_cert:
        app["cert"] = app_cert
    if org_fields:
        init["organizations"][APP_ACME_INDEX].update(org_fields)
    if providers:
        init["providers"] = list(providers)
    if certs:
        init["certs"] = list(certs)
    return init


class Reply:
    """A reply that is not the API envelope (OAuth endpoints, discovery documents); body may be empty."""

    def __init__(self, code, raw):
        self.code = code
        self.raw = raw
        self.json = None
        if raw.strip():
            try:
                self.json = json.loads(raw)
            except ValueError:
                pytest.fail(f"response is not JSON (HTTP {code}): {raw[:300]!r}")

    def __repr__(self):
        return f"<HTTP {self.code} {self.raw[:400]!r}>"


def _send(request, kind):
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return kind(response.status, response.read())
    except urllib.error.HTTPError as failure:
        return kind(failure.code, failure.read())


def form_post(d, path, fields, basic=None, headers=None):
    """POST an application/x-www-form-urlencoded body; basic is (client_id, secret)."""
    head = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    if basic:
        head["Authorization"] = "Basic " + base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
    request = urllib.request.Request(f"http://127.0.0.1:{d.port}{path}", data=urllib.parse.urlencode(fields).encode(),
                                     method="POST", headers=head)
    return _send(request, Reply)


def bare_get(d, path, headers=None):
    request = urllib.request.Request(f"http://127.0.0.1:{d.port}{path}", headers=headers or {})
    return _send(request, Reply)


def upload(d, path, data=b"hello resource\n", content_type="text/plain", owner="acme", user="dana", tag="custom", cred="acme",
           **query):
    """POST /api/upload-resource as multipart/form-data with one 'file' part; extra query parameters by keyword."""
    boundary = "----dirtest" + uuid.uuid4().hex
    filename = path.rsplit("/", 1)[-1] or "file"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    params = {"owner": owner, "user": user, "application": TENANTS[cred]["app"], "tag": tag, "fullFilePath": path, **query}
    t = TENANTS[cred]
    request = urllib.request.Request(
        f"http://127.0.0.1:{d.port}/api/upload-resource?{urllib.parse.urlencode(params)}", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Basic " + base64.b64encode(f"{t['client_id']}:{t['secret']}".encode()).decode()})
    return _send(request, Response)


def jwt_part(token, index):
    part = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def b64url_int(value):
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


def rs256_valid(token, n, e):
    """Whether an RS256 JWT's signature verifies with the RSA public key (n, e)."""
    signing_input, _, signature = token.rpartition(".")
    sig = b64url_int(signature)
    size = (n.bit_length() + 7) // 8
    block = pow(sig, e, n).to_bytes(size, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input.encode()).digest()
    return block == b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
