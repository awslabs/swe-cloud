"""Small task-local HTTP/WebSocket client helpers and comparison rules."""

import hashlib
import json
import socket
import time
from decimal import Decimal


class Response:
    def __init__(self, status, headers, body, raw):
        self.status, self.headers, self.body, self.raw = status, headers, body, raw

    def header(self, name):
        values = [v for k, v in self.headers if k.lower() == name.lower()]
        assert len(values) <= 1, f"duplicate {name} headers: {values}"
        return values[0] if values else None

    def __repr__(self):
        return f"Response({self.status}, {self.headers}, {self.body!r})"


def parse_head(data):
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:] if ":" in line]
    return status, headers, rest


def request(port, method, path, body=None, headers=(), host="127.0.0.1", timeout=10):
    """One request per connection (Connection: close); returns the parsed response."""
    head = f"{method} {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in headers)
    if body is not None:
        head += f"Content-Length: {len(body)}\r\n"
    data = head.encode("latin-1") + b"\r\n" + (body or b"")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            out += chunk
    assert out, f"no response to {method} {path}"
    status, hdrs, rest = parse_head(out)
    r = Response(status, hdrs, rest, out)
    length = r.header("Content-Length")
    if length is not None:
        assert int(length) == len(rest), f"Content-Length {length} but body has {len(rest)} bytes: {r!r}"
    return r


def get(port, path, **kw):
    return request(port, "GET", path, **kw)


# --- comparison under the public equivalence policy ---------------------------------
# JSON documents are compared as values: object key order and whitespace are free,
# numbers compare by numeric value (1, 1.0 and 1e0 are equal), booleans and null are
# distinct from numbers, and strings compare exactly.

def loads(data):
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    return json.loads(data, parse_int=Decimal, parse_float=Decimal)


def same(observed, expected, where="$"):
    if expected is None:
        assert observed is None, f"{where}: expected null, got {observed!r}"
    elif isinstance(expected, bool):
        assert type(observed) is bool and observed is expected, f"{where}: expected {expected!r}, got {observed!r}"
    elif isinstance(expected, (int, float, Decimal)):
        assert isinstance(observed, (int, float, Decimal)) and not isinstance(observed, bool), \
            f"{where}: expected number {expected!r}, got {observed!r}"
        assert Decimal(observed) == Decimal(expected), f"{where}: expected {expected!r}, got {observed!r}"
    elif isinstance(expected, str):
        assert type(observed) is str and observed == expected, f"{where}: expected {expected!r}, got {observed!r}"
    elif isinstance(expected, bytes):
        assert type(observed) is bytes and observed == expected, f"{where}: expected {expected!r}, got {observed!r}"
    elif isinstance(expected, list):
        assert isinstance(observed, list) and len(observed) == len(expected), f"{where}: expected {expected!r}, got {observed!r}"
        for i, (o, e) in enumerate(zip(observed, expected)):
            same(o, e, f"{where}[{i}]")
    elif isinstance(expected, dict):
        assert isinstance(observed, dict) and set(observed) == set(expected), f"{where}: expected {expected!r}, got {observed!r}"
        for k in expected:
            same(observed[k], expected[k], f"{where}.{k}")
    else:
        raise TypeError(expected)


def assert_json(response, expected, content_type="application/json"):
    assert response.status == 200, response
    assert response.header("Content-Type") == content_type, response
    same(loads(response.body), expected)


def assert_jsonp(body, callback, expected):
    text = body.decode("utf-8")
    prefix, suffix = callback + "(", ");\n"
    assert text.startswith(prefix) and text.endswith(suffix), f"bad JSONP envelope: {text!r}"
    same(loads(text[len(prefix):-len(suffix)]), expected)


def unpack(data):
    """MessagePack decode; str and bin families both decode to bytes."""
    import msgpack
    return msgpack.unpackb(data, raw=True, strict_map_key=False)


def same_msgpack(observed, expected, where="$"):
    """Like same(), with strings expressed as bytes (MessagePack str or bin)."""
    def conv(v):
        if isinstance(v, str):
            return v.encode()
        if isinstance(v, list):
            return [conv(x) for x in v]
        if isinstance(v, dict):
            return {conv(k): conv(x) for k, x in v.items()}
        return v
    expected = conv(expected)
    if isinstance(expected, dict):
        assert isinstance(observed, dict) and set(observed) == set(expected), f"{where}: expected {expected!r}, got {observed!r}"
        for k in expected:
            same_msgpack(observed[k], expected[k], f"{where}.{k!r}")
    elif isinstance(expected, list):
        assert isinstance(observed, list) and len(observed) == len(expected), f"{where}: expected {expected!r}, got {observed!r}"
        for i, (o, e) in enumerate(zip(observed, expected)):
            same_msgpack(o, e, f"{where}[{i}]")
    else:
        same(observed, expected, where)


def etag_of(body):
    return '"' + hashlib.md5(body).hexdigest() + '"'


# --- chunked streaming --------------------------------------------------------------

class Stream:
    """An open streaming request (e.g. SUBSCRIBE); parses the head and chunks incrementally."""

    def __init__(self, port, path, headers=(), host="127.0.0.1"):
        self.sock = socket.create_connection((host, port), timeout=10)
        head = f"GET {path} HTTP/1.1\r\nHost: test\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
        self.sock.sendall(head.encode("latin-1"))
        self.buffer = b""
        self.status = self.headers = None

    def _fill(self, deadline):
        self.sock.settimeout(max(0.05, deadline - time.time()))
        data = self.sock.recv(65536)
        if not data:
            raise EOFError("stream closed")
        self.buffer += data

    def read_head(self, timeout=10):
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in self.buffer:
            self._fill(deadline)
        self.status, self.headers, self.buffer = parse_head(self.buffer)
        return self.status

    def header(self, name):
        return Response(self.status, self.headers, b"", b"").header(name)

    def next_chunk(self, timeout=10):
        if self.status is None:
            self.read_head(timeout)
        deadline = time.time() + timeout
        while b"\r\n" not in self.buffer:
            self._fill(deadline)
        size_line, _, rest = self.buffer.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        while len(rest) < size + 2:
            self._fill(deadline)
            size_line, _, rest = self.buffer.partition(b"\r\n")
        assert rest[size:size + 2] == b"\r\n", f"malformed chunk: {self.buffer!r}"
        self.buffer = rest[size + 2:]
        return rest[:size]

    def close(self):
        self.sock.close()


def wait_for(predicate, timeout=10, interval=0.1):
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() > deadline:
            return value
        time.sleep(interval)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def own_address(valkey):
    """This container's non-loopback address, as seen by a local server."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((valkey[0], valkey[1]))
        return s.getsockname()[0]
