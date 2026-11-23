"""Transparent local PostgreSQL relay; faults lose a connection, never replay SQL.

The controller can arm one transaction boundary for a pod. 'before' disconnects
before forwarding COMMIT; 'inflight' forwards COMMIT and disconnects the client while the server
continues resolving the transaction.
Both present a connection failure to the application. No production TLS endpoint
is emulated here; the local relay uses PostgreSQL's unencrypted test connection.
"""
import contextlib
import select
import re
import socket
import socketserver
import struct
import threading


def exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError()
        data += chunk
    return data


def packet(sock):
    kind = exact(sock, 1)
    length = exact(sock, 4)
    size = struct.unpack('!I', length)[0]
    if not 4 <= size <= 16 * 1024 * 1024:
        raise ValueError('invalid PostgreSQL frame length')
    return kind, exact(sock, size - 4), length


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, config):
        super().__init__(('127.0.0.1', 0), Handler)
        self.upstream = (config['host'], int(config['port']))
        self.lock = threading.Lock()
        self.armed = {}
        self.evidence = []
        self.connections = set()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def arm(self, pod, action):
        assert action in ('before', 'inflight')
        with self.lock:
            if pod in self.armed:
                raise RuntimeError('previous relay fault has not fired')
            self.armed[pod] = action

    def consume(self, pod):
        with self.lock:
            return self.armed.pop(pod, None)

    def record(self, pod, action, outcome):
        with self.lock:
            self.evidence.append(dict(pod=pod, action=action, outcome=outcome))

    def close(self):
        self.shutdown()
        self.server_close()
        with self.lock:
            sockets = list(self.connections)
        for conn in sockets:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
        self.thread.join(timeout=3)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        try:
            upstream = socket.create_connection(self.server.upstream, timeout=5)
            upstream.settimeout(None)
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.server.lock:
                self.server.connections.update((client, upstream))
            # The first client packet is untyped (startup parameters).
            length = exact(client, 4)
            size = struct.unpack('!I', length)[0]
            startup = exact(client, size - 4)
            if startup[:4] == struct.pack('!I', 80877102):
                # PostgreSQL CancelRequest is a separate unauthenticated wire
                # connection bearing the backend's secret, not a new login.
                # Workers can cancel their own SQL, including an orphan COMMIT.
                upstream.sendall(length + startup)
                return
            if startup[:4] != struct.pack('!I', 196608):
                raise ValueError('local relay expects sslmode=disable')
            fields = startup[4:].rstrip(b'\0').split(b'\0')
            params = dict(zip(fields[::2], fields[1::2]))
            pod = params.get(b'user', b'').decode()
            upstream.sendall(length + startup)
            statements = {}
            portals = {}
            ledger = False
            lost_reply = False
            committed = False
            while True:
                readable, _, _ = select.select([upstream] if lost_reply else [client, upstream], [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    kind, body, length = packet(source)
                    if source is client:
                        # Follow executed SQL for simple and extended protocol,
                        # including named prepared statements on reused sessions.
                        query = b''
                        if kind == b'P':
                            name, query_text, _ = body.split(b'\0', 2)
                            statements[name] = query_text
                        elif kind == b'B':
                            portal, statement, _ = body.split(b'\0', 2)
                            portals[portal] = statement
                        elif kind == b'E':
                            portal = body.split(b'\0', 1)[0]
                            query = statements.get(portals.get(portal, b''), b'')
                        elif kind == b'Q':
                            query = body.split(b'\0', 1)[0]
                        sql = re.sub(rb'/\*.*?\*/|--[^\n]*', b' ', query,
                                     flags=re.S).strip().lower()
                        if b'insert into dagster.ledger' in sql:
                            ledger = True
                        if re.search(rb'(^|;)\s*(commit|end)(\s+(work|transaction))?\s*;?$', sql) and ledger:
                            action = self.server.consume(pod)
                            if action == 'before':
                                self.server.record(pod, action, 'not_forwarded')
                                return  # Socket close rolls back the transaction.
                            lost_reply = action == 'inflight'
                            ledger = False
                        if re.search(rb'(^|;)\s*rollback(\s+(work|transaction))?\s*;?$', sql):
                            ledger = False
                        upstream.sendall(kind + length + body)
                        if lost_reply:
                            if kind == b'E':
                                # The Execute's corresponding Sync may still be
                                # in the disconnected client's outbound buffer.
                                upstream.sendall(b'S' + struct.pack('!I', 4))
                            # The client sees failure now, not after COMMIT's
                            # deferred checks and WAL completion. Keep the
                            # server connection alive to observe its real result.
                            client.shutdown(socket.SHUT_RDWR)
                    else:
                        if lost_reply:
                            committed |= kind == b'C' and body == b'COMMIT\0'
                            if kind == b'Z':
                                self.server.record(pod, 'inflight', 'committed' if committed else 'not_committed')
                                return  # Lose every response frame for this COMMIT.
                        else:
                            client.sendall(kind + length + body)
        except (EOFError, OSError):
            pass
        finally:
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.shutdown(socket.SHUT_RDWR)
                upstream.close()
            with self.server.lock:
                self.server.connections.discard(client)
                self.server.connections.discard(upstream)
