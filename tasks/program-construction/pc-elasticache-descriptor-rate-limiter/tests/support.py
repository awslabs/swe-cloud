"""Shared launcher and observation helpers for the descriptor-limiter suite.

Every check goes through the public executable: an HTTP /json or gRPC request to a
running /app/program, plus independent inspection of Valkey state with redis-py.
"""

import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import redis

from harness import start_command

UNIT_SECONDS = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400}
MAX_UINT32 = 4294967295


# ---------------------------------------------------------------- time windows


def settle(*units, margin=20):
    """Sleep past a window boundary when one is closer than `margin` seconds.

    Expected keys and reset durations assume all requests of a test fall in one
    fixed window; this keeps a test from straddling a boundary.
    """
    for _ in range(2):
        for unit in units:
            seconds = UNIT_SECONDS[unit]
            left = seconds - time.time() % seconds
            if seconds > 1 and left < margin:
                time.sleep(left + 0.3)


def window_start(unit, at=None):
    seconds = UNIT_SECONDS[unit]
    now = int(time.time() if at is None else at)
    return now // seconds * seconds


class Span:
    """Wall-clock interval bracketing one request."""

    def __init__(self):
        self.start = time.time()
        self.end = None

    def close(self):
        self.end = time.time()
        return self

    def reset_bounds(self, unit):
        seconds = UNIT_SECONDS[unit]
        return (seconds - int(self.end) % seconds, seconds - int(self.start) % seconds)


# ------------------------------------------------------------ program process


def _free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            sockets.append(s)
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


class Runtime:
    """A root-owned, world-readable configuration tree; the program only reads it."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="limiter-runtime-"))
        self.root.chmod(0o755)

    def write(self, relative, text):
        path = self.root / relative
        for parent in reversed(path.relative_to(self.root).parents):
            directory = self.root / parent
            if not directory.exists():
                directory.mkdir()
                directory.chmod(0o755)
        tmp = path.with_name("." + path.name + ".tmp")
        tmp.write_text(text)
        tmp.chmod(0o644)
        os.replace(tmp, path)
        return path

    def remove(self, relative):
        (self.root / relative).unlink()

    def mkdir(self, relative):
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        for p in [path, *path.relative_to(self.root).parents]:
            (self.root / p).chmod(0o755)
        return path

    def symlink(self, target, link):
        """Atomically point `link` at `target` (both relative to the runtime root)."""
        tmp = self.root / (link + ".new")
        os.symlink(self.root / target, tmp)
        os.replace(tmp, self.root / link)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Response:
    def __init__(self, status, content_type, body):
        self.status = status
        self.content_type = content_type
        self.text = body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class Server:
    """One running /app/program with its own ports and configuration tree."""

    def __init__(self, valkey, runtime, env):
        self.valkey = valkey
        self.runtime = runtime
        self.port, self.grpc_port, self.debug_port = _free_ports(3)
        self.log = tempfile.NamedTemporaryFile(prefix="limiter-log-", delete=False)
        base = {
            "RUNTIME_ROOT": str(runtime.root),
            "RUNTIME_SUBDIRECTORY": "",
            "RUNTIME_WATCH_ROOT": "false",
            "USE_STATSD": "false",
            "DISABLE_STATS": "true",
            "EXPIRATION_JITTER_MAX_SECONDS": "0",
            "REDIS_SOCKET_TYPE": "tcp",
            "REDIS_TYPE": "single",
            "REDIS_URL": valkey["main"],
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "GRPC_HOST": "127.0.0.1",
            "GRPC_PORT": str(self.grpc_port),
            "DEBUG_HOST": "127.0.0.1",
            "DEBUG_PORT": str(self.debug_port),
        }
        for key, value in env.items():
            if value is None:
                base.pop(key, None)
            else:
                base[key] = str(value)
        self.env = base
        self.process = None
        self._channel = None

    def start(self, timeout=25):
        self.process = start_command(["/app/program"], env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"/app/program exited with {self.process.returncode} before serving:\n{self.log_tail()}")
            try:
                status, _, body = self._http("GET", "/healthcheck", None, timeout=2)
                if status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError(f"/healthcheck never answered 200 within {timeout}s:\n{self.log_tail()}")

    def log_tail(self, lines=30):
        try:
            return "\n".join(Path(self.log.name).read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "<no log>"

    def stop(self):
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
        Path(self.log.name).unlink(missing_ok=True)

    # ---- HTTP

    def _http(self, method, path, data, timeout=10):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, reply.headers.get("Content-Type", ""), reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), error.read()

    def get(self, path):
        return Response(*self._http("GET", path, None))

    def post_raw(self, data, path="/json"):
        if isinstance(data, str):
            data = data.encode()
        return Response(*self._http("POST", path, data))

    def post(self, body):
        """POST a JSON request; return (Response, Span bracketing it)."""
        span = Span()
        reply = self.post_raw(json.dumps(body))
        return reply, span.close()

    # ---- gRPC

    def grpc(self):
        import grpc

        pb, pbg = rls_modules()
        if self._channel is None:
            self._channel = grpc.insecure_channel(f"127.0.0.1:{self.grpc_port}")
        return pbg.RateLimitServiceStub(self._channel), pb

    def grpc_channel(self):
        self.grpc()
        return self._channel


_RLS = None


def rls_modules():
    """Compile the public proto subset once into a private directory."""
    global _RLS
    if _RLS is None:
        import grpc_tools
        from grpc_tools import protoc

        out = tempfile.mkdtemp(prefix="limiter-proto-")
        shutil.copy(Path(__file__).with_name("rls_subset.proto"), out)
        include = str(Path(grpc_tools.__file__).parent / "_proto")
        status = protoc.main(["protoc", f"-I{out}", f"-I{include}", f"--python_out={out}", f"--grpc_python_out={out}", f"{out}/rls_subset.proto"])
        assert status == 0, "protoc failed"
        sys.path.insert(0, out)
        import rls_subset_pb2
        import rls_subset_pb2_grpc

        _RLS = (rls_subset_pb2, rls_subset_pb2_grpc)
    return _RLS


# -------------------------------------------------------------- Valkey state


def client(address):
    host, port = address.rsplit(":", 1)
    return redis.Redis(host=host, port=int(port), decode_responses=True, socket_timeout=10)


def counters(address, prefix):
    """Every key beginning with `prefix` on one instance, mapped to its integer value."""
    c = client(address)
    found = {}
    for key in c.scan_iter(match=_glob_escape(prefix) + "*", count=500):
        value = c.get(key)
        if value is not None:
            found[key] = int(value)
    return found


def ttl(address, key):
    return client(address).ttl(key)


def delete_prefix(address, prefix):
    c = client(address)
    keys = list(c.scan_iter(match=_glob_escape(prefix) + "*", count=500))
    if keys:
        c.delete(*keys)


def _glob_escape(text):
    return re.sub(r"([*?\[\]\\])", r"\\\1", text)


# ------------------------------------------------------- response comparison
#
# Policy (documented in /docs/interface.md): responses follow the proto3 JSON
# mapping with lowerCamelCase field names. Fields holding their default value
# may be omitted or present (limitRemaining 0, requestsPerUnit 0, name "");
# an absent message field may also be null. Integers compare by numeric value
# (1 == 1.0) but booleans are never numbers. Enums are their string names.
# Durations are proto JSON duration strings compared by value.

_DURATION = re.compile(r"^(\d+)(?:\.(\d{1,9}))?s$")


def _populated(value):
    """Whether a JSON value differs from a proto3 default (null, 0, false, "", [], {})."""
    return value not in (None, 0, False, "", [], {})


def _number(value, what):
    assert type(value) in (int, float), f"{what} must be a JSON number, got {value!r}"
    assert float(value).is_integer(), f"{what} must be integral, got {value!r}"
    return int(value)


def duration_seconds(text, what="durationUntilReset"):
    assert isinstance(text, str), f"{what} must be a proto JSON duration string, got {text!r}"
    match = _DURATION.match(text)
    assert match, f"{what} is not a proto JSON duration: {text!r}"
    return int(match.group(1)) + (int(match.group(2).ljust(9, "0")) / 1e9 if match.group(2) else 0)


def normalize_status(status):
    """Reduce one JSON DescriptorStatus to (code, limit, remaining, reset_seconds)."""
    assert isinstance(status, dict), f"status must be an object: {status!r}"
    unknown = {k: v for k, v in status.items() if k not in ("code", "currentLimit", "limitRemaining", "durationUntilReset")}
    assert not any(_populated(v) for v in unknown.values()), f"unexpected populated status fields {unknown!r}"
    code = status.get("code")
    assert code in ("OK", "OVER_LIMIT"), f"status code must be OK or OVER_LIMIT: {status!r}"
    limit = status.get("currentLimit")
    if limit is not None:
        assert isinstance(limit, dict), f"currentLimit must be an object: {status!r}"
        unknown = {k: v for k, v in limit.items() if k not in ("requestsPerUnit", "unit", "name")}
        assert not any(_populated(v) for v in unknown.values()), f"unexpected populated currentLimit fields {unknown!r}"
        name = limit.get("name", "")
        assert isinstance(name, str), f"currentLimit.name must be a string: {limit!r}"
        unit = limit.get("unit")
        assert unit in UNIT_SECONDS, f"currentLimit.unit must be a unit name: {limit!r}"
        limit = (_number(limit.get("requestsPerUnit", 0), "requestsPerUnit"), unit, name)
    remaining = status.get("limitRemaining")
    remaining = 0 if remaining is None else _number(remaining, "limitRemaining")
    reset = status.get("durationUntilReset")
    reset = None if reset is None else duration_seconds(reset)
    return code, limit, remaining, reset


def S(code="OK", limit=None, remaining=0, name=""):
    """Expected descriptor status. `limit` is (requests_per_unit, UNIT) or None."""
    return {"code": code, "limit": None if limit is None else (limit[0], limit[1], name), "remaining": remaining}


OK_BARE = S()
UNLIMITED = S(remaining=MAX_UINT32)


def over(limit, name=""):
    return S("OVER_LIMIT", limit, 0, name)


def ok(limit, remaining, name=""):
    return S("OK", limit, remaining, name)


def check_statuses(statuses, expected, span):
    assert isinstance(statuses, list), f"statuses must be a list: {statuses!r}"
    assert len(statuses) == len(expected), f"expected {len(expected)} statuses, got {statuses!r}"
    for index, (actual, want) in enumerate(zip(statuses, expected)):
        code, limit, remaining, reset = normalize_status(actual)
        got = {"code": code, "limit": limit, "remaining": remaining}
        assert got == want, f"status[{index}]: expected {want}, got {actual!r}"
        if limit is None:
            assert reset is None, f"status[{index}] without a limit must not carry durationUntilReset: {actual!r}"
        else:
            low, high = span.reset_bounds(limit[1])
            # Whole seconds as of the request; sub-second precision may round below by less than one second.
            assert reset is not None and low - 1 < reset <= high, f"status[{index}] durationUntilReset {reset} outside ({low - 1}, {high}]"


def check_response(reply, span, overall, expected):
    """Assert HTTP status, overall code and every descriptor status of a /json reply."""
    want_http = 429 if overall == "OVER_LIMIT" else 200
    assert reply.status == want_http, f"HTTP {reply.status} (expected {want_http}): {reply.text}"
    body = reply.json()
    assert isinstance(body, dict), body
    extra = {k: v for k, v in body.items() if k not in ("overallCode", "statuses")}
    assert not any(_populated(v) for v in extra.values()), f"unexpected populated response fields: {extra!r}"
    assert body.get("overallCode") == overall, f"overallCode: expected {overall}: {body!r}"
    check_statuses(body.get("statuses"), expected, span)
    return body


def grpc_status(status, pb):
    """Reduce one gRPC DescriptorStatus to the same shape as normalize_status."""
    code = pb.RateLimitResponse.Code.Name(status.code)
    limit = None
    if status.HasField("current_limit"):
        cl = status.current_limit
        limit = (cl.requests_per_unit, pb.RateLimitResponse.RateLimit.Unit.Name(cl.unit), cl.name)
    reset = None
    if status.HasField("duration_until_reset"):
        reset = status.duration_until_reset.seconds + status.duration_until_reset.nanos / 1e9
    return code, limit, status.limit_remaining, reset


def check_grpc(response, pb, span, overall, expected):
    assert pb.RateLimitResponse.Code.Name(response.overall_code) == overall, str(response)
    assert len(response.statuses) == len(expected), str(response)
    for index, (actual, want) in enumerate(zip(response.statuses, expected)):
        code, limit, remaining, reset = grpc_status(actual, pb)
        got = {"code": code, "limit": limit, "remaining": remaining}
        assert got == want, f"grpc status[{index}]: expected {want}, got {actual}"
        if limit is None:
            assert reset is None, f"grpc status[{index}] without limit carries a reset: {actual}"
        else:
            low, high = span.reset_bounds(limit[1])
            assert reset is not None and low - 1 < reset <= high, f"grpc status[{index}] reset {reset} outside ({low - 1}, {high}]"


def d(*pairs, **extra):
    """A request descriptor from (key, value) pairs plus optional JSON fields."""
    return {"entries": [{"key": k, "value": v} for k, v in pairs], **extra}


def config(domain, body):
    """A YAML configuration file for `domain` whose descriptor list is `body`."""
    return f"domain: {domain}\ndescriptors:\n" + textwrap.indent(textwrap.dedent(body).strip("\n"), "  ") + "\n"
