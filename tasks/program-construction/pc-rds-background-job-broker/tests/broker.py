"""Test support: a small job-protocol client, a server launcher and a queue-table reader.

Everything here talks to /app/program only through its TCP port and to MySQL with PyMySQL.
"""

import os
import re
import signal
import socket
import struct
import subprocess
import tempfile
import time
import uuid
from decimal import Decimal

import pymysql

from harness import start_command

CMD = {
    "CAN_DO": 1, "CANT_DO": 2, "RESET_ABILITIES": 3, "PRE_SLEEP": 4, "NOOP": 6, "SUBMIT_JOB": 7,
    "JOB_CREATED": 8, "GRAB_JOB": 9, "NO_JOB": 10, "JOB_ASSIGN": 11, "WORK_STATUS": 12,
    "WORK_COMPLETE": 13, "WORK_FAIL": 14, "GET_STATUS": 15, "ECHO_REQ": 16, "ECHO_RES": 17,
    "SUBMIT_JOB_BG": 18, "ERROR": 19, "STATUS_RES": 20, "SUBMIT_JOB_HIGH": 21, "SET_CLIENT_ID": 22,
    "GRAB_JOB_UNIQ": 30, "JOB_ASSIGN_UNIQ": 31, "SUBMIT_JOB_HIGH_BG": 32, "SUBMIT_JOB_LOW": 33,
    "SUBMIT_JOB_LOW_BG": 34, "SUBMIT_JOB_EPOCH": 36,
}
NAME = {code: name for name, code in CMD.items()}
QUEUE_COLUMNS = ("unique_key", "function_name", "priority", "data", "when_to_run")


def _bytes(value):
    return value if isinstance(value, bytes) else str(value).encode()


class Conn:
    """One binary-protocol connection."""

    def __init__(self, port, timeout=10):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.buffer = b""

    def close(self):
        self.sock.close()

    def send(self, command, *args):
        body = b"\0".join(_bytes(arg) for arg in args)
        self.sock.sendall(b"\0REQ" + struct.pack(">II", CMD[command], len(body)) + body)

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError("server closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def recv(self, timeout=10):
        """Next response packet as (name, payload bytes)."""
        self.sock.settimeout(timeout)
        try:
            magic, code, size = struct.unpack(">4sII", self._read(12))
            payload = self._read(size)
        finally:
            self.sock.settimeout(10)
        assert magic == b"\0RES", f"bad response magic {magic!r}"
        return NAME.get(code, code), payload

    def nothing_pending(self, wait=0.5):
        """True when no packet arrives within `wait` seconds."""
        if self.buffer:
            return False
        self.sock.settimeout(wait)
        try:
            chunk = self.sock.recv(1 << 20)
        except (socket.timeout, TimeoutError):
            return True
        finally:
            self.sock.settimeout(10)
        self.buffer += chunk
        return False

    # client helpers
    def submit(self, command, function, unique, data, epoch=None):
        args = [function, unique] + ([epoch] if epoch is not None else []) + [data]
        self.send(command, *args)
        return self.recv()

    def submit_ok(self, command, function, unique, data, epoch=None):
        kind, payload = self.submit(command, function, unique, data, epoch)
        assert kind == "JOB_CREATED", (kind, payload)
        assert payload, "empty job handle"
        return payload

    def status(self, handle):
        self.send("GET_STATUS", handle)
        kind, payload = self.recv()
        assert kind == "STATUS_RES", (kind, payload)
        return payload.split(b"\0")

    # worker helpers
    def can_do(self, *functions):
        for function in functions:
            self.send("CAN_DO", function)

    def grab(self, unique=True):
        """GRAB_JOB_UNIQ (or GRAB_JOB); returns the assignment fields, or None for NO_JOB."""
        self.send("GRAB_JOB_UNIQ" if unique else "GRAB_JOB")
        while True:
            kind, payload = self.recv()
            if kind == "NOOP":
                continue
            if kind == "NO_JOB":
                assert payload == b"", payload
                return None
            if unique:
                assert kind == "JOB_ASSIGN_UNIQ", (kind, payload)
                return payload.split(b"\0", 3)
            assert kind == "JOB_ASSIGN", (kind, payload)
            return payload.split(b"\0", 2)


def sync(conn):
    """Round-trip an ECHO so every earlier packet on `conn` has been processed."""
    token = uuid.uuid4().hex.encode()
    conn.send("ECHO_REQ", token)
    assert conn.recv() == ("ECHO_RES", token)


def is_error(response, code):
    kind, payload = response
    return kind == "ERROR" and payload.split(b"\0", 1)[0] == code


class Admin:
    """One text admin connection."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.buffer = b""

    def close(self):
        self.sock.close()

    def command(self, line):
        """Lines of the response before the terminating '.' line."""
        self.sock.sendall(line.encode() + b"\n")
        lines = []
        while True:
            while b"\n" not in self.buffer:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise EOFError(f"connection closed after {lines!r}")
                self.buffer += chunk
            raw, self.buffer = self.buffer.split(b"\n", 1)
            text = raw.decode().rstrip("\r")
            if text == ".":
                return lines
            lines.append(text)

    def status(self):
        """Multiset of (function, total, running, available workers) as a sorted list."""
        rows = []
        for line in self.command("status"):
            fields = line.split("\t")
            assert len(fields) == 4, line
            name, *counts = fields
            assert all(re.fullmatch(r"\d+", c) for c in counts), line
            rows.append((name, *map(int, counts)))
        return sorted(rows)

    def workers(self):
        """Sorted list of (ip, client id, sorted functions), one per connection line."""
        rows = []
        for line in self.command("workers"):
            match = re.fullmatch(r"(\d+) (\S+) (\S+) :(.*)", line)
            assert match, f"malformed workers line {line!r}"
            rows.append((match[2], match[3], tuple(sorted(match[4].split()))))
        return sorted(rows)


def wait_for(predicate, timeout=5, interval=0.1):
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() > deadline:
            return value
        time.sleep(interval)


# ---------------------------------------------------------------- MySQL


def connect(mysql, database=True):
    return pymysql.connect(
        host=mysql["host"], port=mysql["port"], user=mysql["user"], password=mysql["password"],
        database=mysql["db"] if database else None, autocommit=True, connect_timeout=10,
    )


def query(mysql, sql, *args):
    with connect(mysql) as con, con.cursor() as cur:
        cur.execute(sql, args or None)
        return [tuple(row) for row in cur.fetchall()]


def _text(value):
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


def _integer(value):
    # Stored numbers compare by integer value; booleans and non-integral values are kept distinct.
    if type(value) is int:
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return ("not an integer", value)


def _blob(value):
    # An empty workload may be stored as SQL NULL or as an empty value.
    return b"" if value is None else bytes(value)


def rows(mysql, table):
    """The queue table as a sorted list of (unique_key, function_name, priority, data, when_to_run)."""
    raw = query(mysql, f"SELECT {', '.join(QUEUE_COLUMNS)} FROM `{table}`")
    return sorted(((_text(u), _text(f), _integer(p), _blob(d), _integer(w)) for u, f, p, d, w in raw), key=repr)


def row(unique, function, priority, data, when=0):
    return (unique, function, priority, data, when)


def expect_rows(mysql, table, *expected):
    assert rows(mysql, table) == sorted(expected, key=repr)


def table_exists(mysql, table):
    return bool(query(mysql, "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", mysql["db"], table))


def drop_table(mysql, table):
    query(mysql, f"DROP TABLE IF EXISTS `{table}`")


# ---------------------------------------------------------------- server


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def program_args(mysql, table, port, directory, *, equals=False, extra=()):
    options = [
        ("--queue-type" if equals else "-q", "MySQL"),
        ("--mysql-host", mysql["host"]), ("--mysql-port", mysql["port"]),
        ("--mysql-user", mysql["user"]), ("--mysql-password", mysql["password"]),
        ("--mysql-db", mysql["db"]),
    ]
    if table is not None:
        options.append(("--mysql-table", table))
    options += [
        ("--port" if equals else "-p", port), ("--listen" if equals else "-L", "127.0.0.1"),
        ("--log-file", os.path.join(directory, f"server-{port}.log")), ("--pid-file", os.path.join(directory, f"server-{port}.pid")),
    ]
    options += list(extra)
    if equals:
        return [f"{name}={value}" for name, value in options]
    return [str(item) for pair in options for item in pair]


class Broker:
    """One /app/program server process on a private port and queue table."""

    def __init__(self, mysql, table, directory, *, equals=False, extra=()):
        self.mysql, self.table, self.directory, self.equals, self.extra = mysql, table, str(directory), equals, extra
        self.port = free_port()
        self.process = None
        self.stderr = None
        self.conns = []

    def start(self):
        assert self.process is None
        self.stderr = tempfile.TemporaryFile()
        self.process = start_command(
            ["/app/program", *program_args(self.mysql, self.table, self.port, self.directory, equals=self.equals, extra=self.extra)],
            cwd=self.directory, stdout=subprocess.DEVNULL, stderr=self.stderr,
        )
        deadline = time.time() + 30
        while True:
            if self.process.poll() is not None:
                code = self.process.returncode
                self.stderr.seek(0)
                message = self.stderr.read().decode(errors="replace")
                self.process = None
                raise AssertionError(f"server exited during startup with {code}: {message}")
            try:
                probe = Conn(self.port, timeout=2)
                try:
                    probe.send("ECHO_REQ", b"ready")
                    if probe.recv(timeout=2) == ("ECHO_RES", b"ready"):
                        return self
                finally:
                    probe.close()
            except (OSError, EOFError):
                pass
            if time.time() > deadline:
                raise AssertionError("server did not answer ECHO_REQ within 30 seconds")
            time.sleep(0.1)

    def stop(self, sig=signal.SIGTERM):
        """Signal the server and return its exit status."""
        for conn in self.conns:
            conn.close()
        self.conns = []
        process, self.process = self.process, None
        if process is None:
            return None
        process.send_signal(sig)
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise AssertionError("server did not exit within 15 seconds of the signal") from None

    def restart(self):
        status = self.stop()
        assert status == 0, f"server exit status {status} after SIGTERM"
        return self.start()

    def kill(self):
        for conn in self.conns:
            conn.close()
        self.conns = []
        if self.process is not None:
            self.process.kill()
            self.process.wait()
            self.process = None

    def conn(self):
        conn = Conn(self.port)
        self.conns.append(conn)
        return conn

    def admin(self):
        admin = Admin(self.port)
        self.conns.append(admin)
        return admin

    def rows(self):
        return rows(self.mysql, self.table)

    def expect_rows(self, *expected):
        expect_rows(self.mysql, self.table, *expected)
