"""WebSocket endpoints (adapted from upstream tests/ws-tests.py)."""

import base64
import hashlib
import json
import os
import socket
import struct

import pytest
import websocket

from gw import loads, same

FORBIDDEN = {"message": "Forbidden", "error": True, "http_status": 403}


def connect(g, path="/.json", headers=None):
    return websocket.create_connection(f"ws://127.0.0.1:{g.port}{path}", timeout=10, header=headers or [])


def text(data):
    return data.decode("utf-8") if isinstance(data, bytes) else data


def ask(ws, *args):
    ws.send(json.dumps(list(args)))
    return loads(text(ws.recv()))


def ask_raw(ws, payload):
    ws.send(payload)
    data = ws.recv()
    return data.encode("utf-8") if isinstance(data, str) else data


def resp(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


@pytest.mark.timeout(600)
def test_json_socket_commands_and_state(gateway, db, key):
    g = gateway()
    for path in ("/", "/.json"):
        ws = connect(g, path)
        try:
            same(ask(ws, "PING"), {"PING": [True, "PONG"]})
            same(ask(ws, "SET", f"{key}:w{len(path)}", "via-ws"), {"SET": [True, "OK"]})
            assert db(0).get(f"{key}:w{len(path)}") == b"via-ws"
            same(ask(ws, "GET", f"{key}:w{len(path)}"), {"GET": "via-ws"})
        finally:
            ws.close()
    ws = connect(g)
    try:
        same(ask(ws, "GET", f"{key}:none"), {"GET": None})
        same(ask(ws, "MGET", f"{key}:w1", f"{key}:none"), {"MGET": ["via-ws", None]})
        same(ask(ws, "HSET", f"{key}:h", "f", "v"), {"HSET": 1})
        same(ask(ws, "HGETALL", f"{key}:h"), {"HGETALL": {"f": "v"}})
        same(ask(ws, "set", f"{key}:lc", "a/b%2fc.raw"), {"set": [True, "OK"]})
        assert db(0).get(f"{key}:lc") == b"a/b%2fc.raw"  # no URL decoding or extension handling
        same(ask(ws, "SET", f"{key}:empty", ""), {"SET": [True, "OK"]})
        assert db(0).get(f"{key}:empty") == b""
        obj = ask(ws, "NOSUCH")
        assert list(obj) == ["NOSUCH"] and obj["NOSUCH"][0] is False
        same(ask(ws, "GET", f"{key}:h"), {"GET": [False, "WRONGTYPE Operation against a key holding the wrong kind of value"]})
    finally:
        ws.close()


def test_json_socket_many_sequential_commands(gateway, db, key):
    g = gateway()
    ws = connect(g)
    try:
        for i in range(100):
            same(ask(ws, "LPUSH", f"{key}:l", f"value-{i}"), {"LPUSH": i + 1})
        same(ask(ws, "LLEN", f"{key}:l"), {"LLEN": 100})
        assert db(0).lindex(f"{key}:l", 0) == b"value-99"
    finally:
        ws.close()


@pytest.mark.parametrize("size", [1024, 2 ** 18])
def test_json_socket_large_frames(gateway, db, key, size):
    g = gateway()
    ws = connect(g)
    value = "A" * (size - 7) + "end-tag"
    try:
        same(ask(ws, "SET", f"{key}:big", value), {"SET": [True, "OK"]})
        assert db(0).get(f"{key}:big") == value.encode()
        same(ask(ws, "GET", f"{key}:big"), {"GET": value})
    finally:
        ws.close()


def test_raw_socket_speaks_resp(gateway, db, key):
    g = gateway()
    ws = connect(g, "/.raw")
    try:
        assert ask_raw(ws, resp("PING")) == b"+PONG\r\n"
        assert ask_raw(ws, resp("SET", f"{key}:r", "hi there")) == b"+OK\r\n"
        assert db(0).get(f"{key}:r") == b"hi there"
        assert ask_raw(ws, resp("GET", f"{key}:r")) == b"$8\r\nhi there\r\n"
        assert ask_raw(ws, resp("GET", f"{key}:none")) == b"$-1\r\n"
        assert ask_raw(ws, resp("RPUSH", f"{key}:l", "x", "y")) == b":2\r\n"
        assert ask_raw(ws, resp("LRANGE", f"{key}:l", "0", "-1")) == b"*2\r\n$1\r\nx\r\n$1\r\ny\r\n"
        assert ask_raw(ws, resp("GET", f"{key}:l")) == b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"
    finally:
        ws.close()


def test_acl_on_sockets(gateway, db, key):
    g = gateway(acl=[{"disabled": ["DEBUG", "SETEX"]}])
    ws = connect(g)
    try:
        same(ask(ws, "SETEX", f"{key}:x", "100", "v"), FORBIDDEN)
        same(ask(ws, "DEBUG", "OBJECT", f"{key}:x"), FORBIDDEN)
        same(ask(ws, "MULTI"), FORBIDDEN)
        assert db(0).exists(f"{key}:x") == 0
        # the socket stays usable after a refusal
        same(ask(ws, "SET", f"{key}:ok", "1"), {"SET": [True, "OK"]})
    finally:
        ws.close()
    ws = connect(g, "/.raw")
    try:
        assert ask_raw(ws, resp("SETEX", f"{key}:x", "100", "v")) == b"-ERR Forbidden\r\n"
        assert ask_raw(ws, resp("MULTI")) == b"-ERR Forbidden\r\n"
        assert ask_raw(ws, resp("SET", f"{key}:ok2", "1")) == b"+OK\r\n"
    finally:
        ws.close()
    assert db(0).exists(f"{key}:x") == 0 and db(0).get(f"{key}:ok2") == b"1"


def test_handshake_basic_auth_applies_to_socket_commands(gateway, db, key):
    g = gateway(acl=[{"disabled": ["SETEX"]}, {"http_basic_auth": "u:p", "enabled": ["SETEX"]}])
    auth = "Authorization: Basic " + base64.b64encode(b"u:p").decode()
    ws = connect(g, headers=[auth])
    try:
        same(ask(ws, "SETEX", f"{key}:a", "100", "v"), {"SETEX": [True, "OK"]})
    finally:
        ws.close()
    assert db(0).get(f"{key}:a") == b"v"
    ws = connect(g, headers=["Authorization: Basic " + base64.b64encode(b"u:bad").decode()])
    try:
        same(ask(ws, "SETEX", f"{key}:b", "100", "v"), FORBIDDEN)
    finally:
        ws.close()
    assert db(0).exists(f"{key}:b") == 0


def test_socket_publish_subscribe(gateway, key):
    g = gateway()
    sub, pub = connect(g), connect(g)
    channels = [f"{key}:c1", f"{key}:c2"]
    try:
        for i, ch in enumerate(channels, 1):
            same(ask(sub, "SUBSCRIBE", ch), {"SUBSCRIBE": ["subscribe", ch, i]})
        for i in range(4):
            for ch in channels:
                same(ask(pub, "PUBLISH", ch, f"message-{i}"), {"PUBLISH": 1})
        received = [loads(text(sub.recv())) for _ in range(8)]
        expected = [{"SUBSCRIBE": ["message", ch, f"message-{i}"]} for i in range(4) for ch in channels]
        for got, want in zip(received, expected):
            same(got, want)
        # ordinary commands are refused while subscribed
        sub.send(json.dumps(["GET", f"{key}:x"]))
        assert text(sub.recv()) == "Command not allowed after subscribe"
        same(ask(sub, "UNSUBSCRIBE", channels[0]), {"UNSUBSCRIBE": ["unsubscribe", channels[0], 1]})
        same(ask(pub, "PUBLISH", channels[0], "gone"), {"PUBLISH": 0})
        same(ask(pub, "PUBLISH", channels[1], "still"), {"PUBLISH": 1})
        # later deliveries are keyed by the most recent command sent on the socket
        same(loads(text(sub.recv())), {"UNSUBSCRIBE": ["message", channels[1], "still"]})
    finally:
        sub.close()
        pub.close()


def test_invalid_json_frames_close_the_socket(gateway):
    g = gateway()
    for bad in ('{"a":1}', '"PING"', "[]", "not json"):
        ws = connect(g)
        try:
            ws.send(bad)
            with pytest.raises((websocket.WebSocketConnectionClosedException, ConnectionError)):
                ws.recv()
        finally:
            ws.close()
    ws = connect(g)
    try:
        # non-string elements are skipped
        same(ask(ws, "PING", ["x"], {"a": 1}), {"PING": [True, "PONG"]})
    finally:
        ws.close()


def handshake(g, path="/.json"):
    s = socket.create_connection(("127.0.0.1", g.port), timeout=10)
    key = base64.b64encode(os.urandom(16))
    s.sendall(b"GET " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: Upgrade\r\nUpgrade: WebSocket\r\n"
              b"Origin: http://127.0.0.1\r\nSec-WebSocket-Key: " + key + b"\r\n\r\n")
    head = b""
    while b"\r\n\r\n" not in head:
        data = s.recv(1)
        assert data, "closed during handshake"
        head += data
    lines = head.decode().split("\r\n")
    assert lines[0].split(" ")[1] == "101", lines
    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).decode()
    fields = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
    assert fields.get("sec-websocket-accept") == accept, lines
    return s


def recv_exact(s, n):
    out = b""
    while len(out) < n:
        data = s.recv(n - len(out))
        if not data:
            break
        out += data
    return out


def test_ping_frame_gets_pong(gateway):
    g = gateway()
    with handshake(g) as s:
        s.sendall(bytes([0x89, 0x80, 0x12, 0x34, 0x56, 0x78]))
        assert recv_exact(s, 2) == bytes([0x8A, 0x00])


def test_unmasked_frame_is_a_protocol_error(gateway):
    g = gateway()
    with handshake(g) as s:
        payload = b'["PING"]'
        s.sendall(bytes([0x81, len(payload)]) + payload)
        header = recv_exact(s, 2)
        assert header[0] == 0x88
        body = recv_exact(s, header[1] & 0x7F)
        assert struct.unpack(">H", body[:2])[0] == 1002


def test_oversized_frame_closes_connection(gateway):
    g = gateway()
    with handshake(g) as s:
        s.sendall(bytes([0x82, 0xFF]) + struct.pack(">Q", 2 ** 48) + os.urandom(4))
        try:
            data = s.recv(1)
        except ConnectionResetError:
            data = b""
        assert data == b""
