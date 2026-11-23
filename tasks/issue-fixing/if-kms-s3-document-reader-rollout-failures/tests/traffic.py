import http.client
import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote
from protocol import ENDPOINT


def decoded_body(body, encoding):
    if 'aws-chunked' not in encoding:
        return body
    result = bytearray()
    while body:
        line, body = body.split(b'\r\n', 1)
        n = int(line.split(b';')[0], 16)
        if not n:
            return bytes(result)
        result.extend(body[:n])
        body = body[n + 2:]
    return bytes(result)


class Traffic:
    def __init__(self, corpus):
        self.c = corpus
        self.errors, self.audit = [], []
        self.processes = {}
        self.killed = {}
        self.edited = False
        self.archive_edited = False
        self.denials = []
        self.retired = False
        self.servers = []
        self.announcements = []

    def error(self, error):
        if len(self.errors) < 100:
            self.errors.append(str(error))

    def proxy(self, role):
        state = self
        upstream = urlsplit(ENDPOINT)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            disable_nagle_algorithm = True

            def log_message(self, *_):
                pass

            def body(self):
                if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                    result = bytearray()
                    while True:
                        n = int(self.rfile.readline().split(b';')[0], 16)
                        if n == 0:
                            while self.rfile.readline().strip():
                                pass
                            return bytes(result)
                        result.extend(self.rfile.read(n))
                        self.rfile.read(2)
                return self.rfile.read(int(self.headers.get('Content-Length', 0)))

            def request(self):
                payload = self.body()
                target = self.headers.get('X-Amz-Target', '').split('.')[-1]
                if self.command == 'POST' and target in ('Encrypt', 'ReEncrypt', 'GenerateDataKey',
                                                        'GenerateDataKeyWithoutPlaintext'):
                    request = json.loads(payload)
                    arn = request.get('DestinationKeyId') if target == 'ReEncrypt' else request.get('KeyId')
                    # Model the stable, public gateway tier independently of any
                    # solver-mutated transport/permissions module. Upstream KMS
                    # remains responsible for encryption and context binding.
                    if arn is not None and arn not in state.c.decrypt_only:
                        try:
                            arn = state.c.kms.describe_key(KeyId=arn)['KeyMetadata']['Arn']
                        except Exception:
                            pass
                    if arn in state.c.decrypt_only:
                        state.denials.append({'target': target, 'arn': arn, 'role': role})
                        body = json.dumps({'__type': 'AccessDeniedException',
                                           'message': 'rollout principal has Decrypt only on ' + arn}).encode()
                        self.send_response(400)
                        self.send_header('Content-Type', 'application/x-amz-json-1.1')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                path = unquote(urlsplit(self.path).path)
                prefix = '/' + state.c.bucket + '/'
                key = path[len(prefix):] if path.startswith(prefix) else None
                doc_put = self.command == 'PUT' and key and key.startswith('docs/')
                # Serialize the uncooperative old writer with the acceptance audit,
                # not with the new client's earlier read/decision.
                lock = state.c.lock if doc_put else threading.RLock()
                with lock:
                    if role == 'reader':
                        target = self.headers.get('X-Amz-Target', '').split('.')[-1]
                        if self.command not in ('GET', 'HEAD') and not (self.command == 'POST' and target == 'Decrypt'):
                            state.error('reader attempted a write')
                    if doc_put and role != 'reader':
                        if key == state.c.keys[0] and role not in state.killed:
                            state.killed[role] = time.monotonic()
                            proc = state.processes.get(role)
                            if proc is not None:
                                proc.kill()
                            self.close_connection = True
                            return
                        if key == state.c.keys[1] and not state.edited:
                            state.edited = True
                            try:
                                state.c.old_write(key, 0)
                            except Exception as exc:
                                state.error(f'legacy edit: {exc}')
                        if key == state.c.archive_key and not state.archive_edited:
                            state.archive_edited = True
                            try:
                                state.c.old_write(key, 1)
                            except Exception as exc:
                                state.error(f'archive edit: {exc}')
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in ('connection', 'transfer-encoding', 'content-length', 'expect')}
                    headers['Content-Length'] = str(len(payload))
                    connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=15)
                    try:
                        connection.request(self.command, self.path, body=payload, headers=headers)
                        response = connection.getresponse()
                        body = response.read()
                        status = response.status
                        response_headers = response.getheaders()
                    except Exception as exc:
                        state.error(f'proxy transport: {exc}')
                        self.send_error(502)
                        return
                    finally:
                        connection.close()
                    if self.command == 'DELETE' and key and key.startswith('docs/') and 200 <= status < 300:
                        state.error('document deletion was accepted')
                    if doc_put and 200 <= status < 300:
                        try:
                            doc = json.loads(decoded_body(payload, self.headers.get('Content-Encoding', '')))
                            state.c.check(key, doc, legacy=not state.retired)
                            state.audit.append({'role': role, 'key': key, 'status': status, 'etag': self.headers.get('If-Match')})
                        except Exception as exc:
                            state.error(f'accepted write: {exc}')
                    self.send_response(status)
                    for k, v in response_headers:
                        if k.lower() not in ('connection', 'transfer-encoding', 'content-length'):
                            self.send_header(k, v)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = request

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return 'http://127.0.0.1:' + str(server.server_port)

    def pauses(self, path):
        state = self

        class Pause(socketserver.StreamRequestHandler):
            def handle(self):
                event = json.loads(self.rfile.readline())
                state.announcements.append(event)
                self.wfile.write(b'go')

        server = socketserver.ThreadingUnixStreamServer(path, Pause)
        server.daemon_threads = True
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
