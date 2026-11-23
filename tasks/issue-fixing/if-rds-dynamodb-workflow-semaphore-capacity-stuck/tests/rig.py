"""Verifier-owned transport and lifecycle drivers. No application imports."""
import http.client
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class TCPProxy:
    def __init__(self, host, port):
        self.target = (host, port)
        self.cut = False
        self.sockets = set()
        self.lock = threading.Lock()
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                peer = None
                try:
                    with proxy.lock:
                        if proxy.cut:
                            return
                        peer = socket.create_connection(proxy.target, 2)
                        pair = (self.request, peer)
                        proxy.sockets.update(pair)
                    while True:
                        ready, _, _ = select.select(pair, [], [], .5)
                        if proxy.cut:
                            return
                        for src in ready:
                            data = src.recv(65536)
                            if not data:
                                return
                            (peer if src is self.request else self.request).sendall(data)
                except OSError:
                    pass
                finally:
                    if peer:
                        with proxy.lock:
                            proxy.sockets.difference_update((self.request, peer))
                        peer.close()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set_cut(self, cut):
        with self.lock:
            self.cut = cut
            if cut:
                for sock in list(self.sockets):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                        sock.close()
                    except OSError:
                        pass
                self.sockets.clear()

    def close(self):
        self.set_cut(True)
        self.server.shutdown()
        self.server.server_close()


class WorkflowProxy:
    def __init__(self, endpoint, table):
        target = urlsplit(endpoint)
        proxy = self
        self.before_write = None
        self.hook_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(raw)
                # Permit ordinary single-table, batch and transactional clients.
                names = []
                if 'TableName' in body:
                    names.append(body['TableName'])
                for action in body.get('TransactItems', []):
                    names.extend(v['TableName'] for v in action.values())
                names.extend(body.get('RequestItems', {}).keys())
                if not names or any(name != table for name in names):
                    payload = json.dumps({'__type': 'ResourceNotFoundException', 'message': 'Cluster API does not contain that table'}).encode()
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/x-amz-json-1.0')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                operation = self.headers.get('X-Amz-Target', '').split('.')[-1]
                writes = []
                if operation in {'UpdateItem','PutItem','DeleteItem'}:
                    writes.append(body.get('Key', body.get('Item', {})))
                elif operation == 'TransactWriteItems':
                    for action in body.get('TransactItems', []):
                        writes.extend(v.get('Key', v.get('Item', {})) for k,v in action.items()
                                      if k != 'ConditionCheck')
                elif operation == 'BatchWriteItem':
                    for action in body.get('RequestItems', {}).get(table, []):
                        writes.extend(v.get('Key', v.get('Item', {})) for v in action.values())
                # A worker publication overlaps an ordinary receipt mutation.
                # Hooks run before forwarding, without holding up controller IO.
                with proxy.hook_lock:
                    if proxy.before_write:
                        proxy.before_write(writes)
                conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=10)
                try:
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in {'host', 'connection'}}
                    conn.request('POST', '/', raw, headers)
                    reply = conn.getresponse()
                    payload = reply.read()
                    self.send_response(reply.status)
                    self.send_header('Content-Type', 'application/x-amz-json-1.0')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    conn.close()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.endpoint = 'http://127.0.0.1:' + str(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Pauses:
    def __init__(self):
        self.path = tempfile.mktemp(prefix='flow-', suffix='.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.server.settimeout(.5)
        self.target = None
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.stop = False
        self.announcements = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while not self.stop:
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        try:
            connection.settimeout(10)
            data = b''
            while b'\n' not in data:
                part = connection.recv(4096)
                if not part:
                    return
                data += part
            facts = json.loads(data.split(b'\n')[0])
            self.announcements.append(facts)
            if facts.get('workflowkey') == self.target and facts.get('seam') == 'after_commit':
                self.arrived.set()
                self.release.wait(45)
            connection.sendall(b'go')
        except OSError:
            pass
        finally:
            connection.close()

    def close(self):
        self.stop = True
        self.release.set()
        self.server.close()
        Path(self.path).unlink(missing_ok=True)


def launch(name, cluster, table, dsn, endpoint, pauses, label):
    path = Path('/logs/verifier') / (label + '.log')
    stream = path.open('w')
    process = subprocess.Popen(
        ['python3', '/app/controller.py', '--name', name, '--cluster', cluster,
         '--workflow-table', table, '--pg-dsn', dsn, '--dynamodb-endpoint', endpoint,
         '--heartbeat-seconds', '2', '--inactive-controller-seconds', '15'],
        cwd=tempfile.mkdtemp(prefix='controller-'),
        env={**os.environ, 'PYTHONPATH': '/app', 'PYTHONDONTWRITEBYTECODE': '1', 'SEAM_SOCKET': pauses.path},
        stdout=stream, stderr=subprocess.STDOUT)
    stream.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if 'READY' in path.read_text():
            return process, time.monotonic(), True
        if process.poll() is not None:
            break
        time.sleep(.1)
    return process, time.monotonic(), False
