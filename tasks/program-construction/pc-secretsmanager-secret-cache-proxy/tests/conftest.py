"""Shared fixtures: unique secrets, running program instances and a recording AWS endpoint."""

import base64
import http.client
import json
import re
import signal
import socket
import subprocess
import time
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError

from awsproxy import Recorder
from harness import start_command, workspace, write_fixture

DEFAULT_HEADER = "X-Aws-Parameters-Secrets-Token"
READY_LINE = re.compile(r"^Secrets Manager Provider/\S+ listening on http://127\.0\.0\.1:(\d+)\s*$", re.M)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def nested_config(port, body="", *, validate=False, log_level="info"):
    """A nested-layout config; `body` continues inside [capabilities.secrets_manager]."""
    return (
        f'[logging]\nlog_to_file = false\nlog_level = "{log_level}"\n\n'
        f"[capabilities.secrets_manager]\nhttp_port = {port}\nvalidate_credentials = {str(validate).lower()}\n" + body
    )


class Agent:
    """One running /app/program instance and an HTTP client for it."""

    def __init__(self, root, logs, config, *, env, args, port, token):
        self.root, self.port, self.token = root, port, token
        if config is not None:
            write_fixture(root / "cfg.toml", config)
        self.stdout_path, self.stderr_path = logs / "stdout.txt", logs / "stderr.txt"
        self._out, self._err = open(self.stdout_path, "wb"), open(self.stderr_path, "wb")
        self.proc = start_command(["/app/program", "sm", "start", *args], cwd=root, env=env, stdout=self._out, stderr=self._err)

    def wait_ready(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                if self.request("GET", "/ping", token=False) == (200, "healthy"):
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    def wait_exit(self, timeout=15):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def request(self, method, path, headers=None, *, token=True, timeout=45):
        sent = {DEFAULT_HEADER: self.token} if token is True else {}
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, headers=sent)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def get(self, path, headers=None, *, token=True):
        return self.request("GET", path, headers, token=token)

    def query(self, secret_id, **params):
        """GET /secretsmanager/get with the given query parameters (values are URL-encoded)."""
        from urllib.parse import quote

        pairs = [("secretId", secret_id)] + [(k, v) for k, v in params.items()]
        return self.get("/secretsmanager/get?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in pairs))

    def stdout(self):
        self._out.flush()
        return self.stdout_path.read_bytes().decode("utf-8", "replace")

    def stderr(self):
        self._err.flush()
        return self.stderr_path.read_bytes().decode("utf-8", "replace")

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        code = self.wait_exit(10)
        if code is None:
            self.proc.kill()
            self.proc.wait()
        self._out.close()
        self._err.close()
        return code


@pytest.fixture
def launch(tmp_path):
    """Start program instances in fresh workspaces; "{root}" in env values names the workspace."""
    agents, scratch = [], []

    def start(config=None, *, env=None, endpoint=None, token="__default__", args=None, port=None, ready=True, files=None):
        context = workspace()
        root = context.__enter__()
        scratch.append(context)
        logs = tmp_path / uuid.uuid4().hex[:8]
        logs.mkdir()
        token = f"tok-{uuid.uuid4().hex[:12]}" if token == "__default__" else token
        environment = {"AWS_TOKEN": token} if token is not None else {}
        if endpoint:
            environment["AWS_ENDPOINT_URL"] = endpoint
        environment.update({key: value.replace("{root}", str(root)) for key, value in (env or {}).items()})
        for name, content in (files or {}).items():
            write_fixture(root / name, content)
        if args is None:
            args = ["--config", "cfg.toml"] if config is not None else []
        agent = Agent(root, logs, config, env=environment, args=args, port=port, token=token)
        agents.append(agent)
        if ready:
            assert agent.wait_ready(), f"program did not become ready\nstdout:\n{agent.stdout()[-2000:]}\nstderr:\n{agent.stderr()[-2000:]}"
        return agent

    yield start
    for agent in agents:
        agent.stop()
    for context in scratch:
        context.__exit__(None, None, None)


@pytest.fixture
def agent(launch, recorder):
    """A ready program with a 300 s TTL whose AWS traffic is recorded."""
    port = free_port()
    started = launch(nested_config(port, "[capabilities.secrets_manager.cache]\nttl_seconds = 300\n"), endpoint=recorder.url, port=port)
    recorder.take()
    return started


@pytest.fixture
def recorder():
    proxy = Recorder()
    yield proxy
    proxy.close()


class Secrets:
    """Create uniquely named secrets and force-delete them afterwards."""

    def __init__(self):
        self.prefix = "scp-" + uuid.uuid4().hex[:10]
        self.clients = {}
        self.owned = []

    def client(self, region="us-east-1"):
        if region not in self.clients:
            self.clients[region] = boto3.client("secretsmanager", region_name=region)
        return self.clients[region]

    def name(self, suffix):
        return f"{self.prefix}-{suffix}"

    def create(self, suffix, region="us-east-1", **kwargs):
        name = self.name(suffix)
        response = self.client(region).create_secret(Name=name, **kwargs)
        self.owned.append((region, name))
        return response

    def cleanup(self):
        for region, name in self.owned:
            try:
                self.client(region).delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            except ClientError:
                pass


@pytest.fixture
def secrets():
    store = Secrets()
    yield store
    store.cleanup()


@pytest.fixture
def sm(secrets):
    return secrets.client()


# ---------------------------------------------------------------- assertions


def document(response):
    """Decode a successful lookup response."""
    status, body = response
    assert status == 200, f"expected 200, got {status}: {body[:500]}"
    value = json.loads(body)
    assert isinstance(value, dict), body[:500]
    return value


def service_value(sm, **kwargs):
    """What Secrets Manager itself returns for the same lookup, as a reference document."""
    return sm.get_secret_value(**kwargs)


def assert_matches(doc, expected):
    """`doc` is the program's JSON object; `expected` a boto3 GetSecretValue (or BatchGet entry) result."""
    payload = "SecretBinary" if "SecretBinary" in expected else "SecretString"
    fields = {"ARN", "Name", "VersionId", payload, "CreatedDate"} | ({"VersionStages"} if "VersionStages" in expected else set())
    assert set(doc) == fields, sorted(doc)
    for key in ("ARN", "Name", "VersionId"):
        assert type(doc[key]) is str and doc[key] == expected[key], (key, doc[key], expected[key])
    if payload == "SecretString":
        assert type(doc["SecretString"]) is str and doc["SecretString"] == expected["SecretString"]
    else:
        assert type(doc["SecretBinary"]) is str and base64.b64decode(doc["SecretBinary"], validate=True) == expected["SecretBinary"]
    if "VersionStages" in expected:
        stages = doc["VersionStages"]
        assert type(stages) is list and all(type(s) is str for s in stages), stages
        assert sorted(stages) == sorted(expected["VersionStages"]), (stages, expected["VersionStages"])
    created = doc["CreatedDate"]
    assert type(created) is str and re.fullmatch(r"\d+(\.\d+)?", created), created
    assert abs(float(created) - expected["CreatedDate"].timestamp()) < 0.0015, (created, expected["CreatedDate"])


def assert_same_document(doc, earlier):
    """A cached answer: the identical JSON object served earlier (stages compared as a multiset)."""
    assert set(doc) == set(earlier)
    for key in earlier:
        if key == "VersionStages":
            assert sorted(doc[key]) == sorted(earlier[key]), (doc[key], earlier[key])
        else:
            assert type(doc[key]) is type(earlier[key]) and doc[key] == earlier[key], (key, doc[key], earlier[key])


def service_error(call):
    """The (status, error document) Secrets Manager returns for a failing boto3 call."""
    with pytest.raises(ClientError) as info:
        call()
    error = info.value.response
    return error["ResponseMetadata"]["HTTPStatusCode"], {"__type": error["Error"]["Code"], "message": error["Error"]["Message"]}


def assert_error(response, status, body):
    got_status, got_body = response
    assert got_status == status, (got_status, got_body[:500])
    value = json.loads(got_body)
    assert value == body and all(type(value[k]) is str for k in value), value


def assert_text(response, status, text):
    got_status, got_body = response
    assert (got_status, got_body.strip()) == (status, text), (got_status, got_body[:300])
