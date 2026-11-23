"""A minimal raw PostgreSQL frontend for observing the wire exchange directly (startup, MD5 challenge, simple Query)."""

import hashlib
import socket
import struct


class Wire:
    def __init__(self, port, timeout=10):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout)
        self.sock.settimeout(timeout)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def startup(self, **params):
        body = struct.pack("!i", 196608)
        for key, value in params.items():
            body += key.encode() + b"\0" + value.encode() + b"\0"
        body += b"\0"
        self.sock.sendall(struct.pack("!i", len(body) + 4) + body)

    def send(self, kind, payload=b""):
        self.sock.sendall(kind + struct.pack("!i", len(payload) + 4) + payload)

    def _exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def read(self):
        """The next (type, body) message, or None at end of stream."""
        head = self._exact(5)
        if head is None:
            return None
        length = struct.unpack("!i", head[1:])[0]
        body = self._exact(length - 4)
        if body is None:
            return None
        return head[:1], body

    def until_ready(self, limit=10000):
        """Messages up to and including ReadyForQuery (or until the stream ends)."""
        messages = []
        for _ in range(limit):
            message = self.read()
            messages.append(message)
            if message is None or message[0] == b"Z":
                break
        return messages

    def until_closed(self, limit=100):
        messages = []
        for _ in range(limit):
            try:
                message = self.read()
            except (ConnectionResetError, socket.timeout):
                messages.append("unterminated")
                break
            messages.append(message)
            if message is None:
                break
        return messages

    def query(self, sql):
        self.send(b"Q", sql.encode() + b"\0")
        return self.until_ready()


def md5_response(user, password, salt):
    inner = hashlib.md5(password.encode() + user.encode()).hexdigest()
    return b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode() + b"\0"


def error_fields(body):
    """ErrorResponse/NoticeResponse body -> {field code: value}."""
    fields = {}
    for part in body.split(b"\0"):
        if part:
            fields[part[:1].decode()] = part[1:].decode()
    return fields


def login(port, database, user, password):
    """Attempt an MD5 login on a raw connection and collect what the server sends.

    Returns (authentication request codes, messages). Messages are read until the server closes the
    connection or, after authentication, until ReadyForQuery.
    """
    codes, messages = [], []
    with Wire(port) as wire:
        wire.startup(user=user, database=database)
        while True:
            try:
                message = wire.read()
            except (ConnectionResetError, socket.timeout):
                break
            if message is None:
                break
            kind, body = message
            if kind == b"R" and struct.unpack("!i", body[:4])[0] == 5 and not codes:
                codes.append(5)
                wire.send(b"p", md5_response(user, password, body[4:8]))
                continue
            if kind == b"R":
                codes.append(struct.unpack("!i", body[:4])[0])
            messages.append(message)
            if kind == b"Z" and 0 in codes:
                break
    return codes, messages


def refusal(port, database, user, password):
    """(authentication request codes, [(severity, SQLSTATE) of each ErrorResponse], message types) of a login."""
    codes, messages = login(port, database, user, password)
    errors = [error_fields(body) for kind, body in messages if kind == b"E"]
    return codes, [(e.get("V"), e.get("C")) for e in errors], [kind for kind, _ in messages]
