"""Verifier-owned wire scheduling; no application modules are imported."""
import http.client
import http.server
import json
import re
import select
import socket
import threading
import time
from urllib.parse import urlsplit, parse_qs, unquote


class Rule:
    def __init__(self, mode=None, fail_abort=False):
        self.mode = mode
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.failed = threading.Event()
        self.failure_release = threading.Event()
        self.fail_abort = fail_abort
        self.used = False
        self.hold = 3
        # 'create' mode: set once the withheld reply was handed back; `unanswered` is the
        # UploadId of that creation when the consumer had already stopped waiting for it.
        self.replied = threading.Event()
        self.unanswered = None


def hung_up(connection):
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(readable) and connection.recv(1, socket.MSG_PEEK) == b''
    except OSError:
        return True


class Relay:
    def __init__(self, upstream, download):
        self.upstream = urlsplit(upstream)
        self.download = download
        self.rules = {}
        self.log = []
        self.lock = threading.Lock()
        self.query_rule = None
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self): self.dispatch()
            def do_HEAD(self): self.dispatch()
            def do_POST(self): self.dispatch()
            def do_PUT(self): self.dispatch()
            def do_DELETE(self): self.dispatch()

            def dispatch(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                path = urlsplit(self.path)
                query = parse_qs(path.query, keep_blank_values=True)
                pieces = unquote(path.path).lstrip('/').split('/', 1)
                key = pieces[1] if len(pieces) == 2 and pieces[0] == owner.download else None
                op = self.command
                if key:
                    if self.command == 'POST' and 'uploads' in query:
                        op = 'create'
                    elif 'uploadId' in query:
                        op = {'POST': 'complete', 'DELETE': 'abort', 'PUT': 'part'}.get(self.command, op)
                    elif self.command == 'PUT':
                        op = 'publish'
                rule = owner.rules.get(key)
                query_rule = None
                if self.headers.get('X-Amz-Target', '').endswith(('.Query', '.Scan')):
                    with owner.lock:
                        query_rule = owner.query_rule
                        owner.query_rule = None
                entry = {'key': key, 'op': op, 'uploadId': query.get('uploadId', [None])[0],
                         'status': None, 'forwarded': False, 'time': time.monotonic()}
                owner.log.append(entry)
                try:
                    if rule and rule.mode == 'stall' and op in ('complete', 'publish'):
                        rule.arrived.set()
                        if not rule.release.wait(45):
                            raise TimeoutError('publish gate timed out')
                    fail = False
                    if rule and op == 'abort':
                        with owner.lock:
                            if rule.fail_abort:
                                rule.fail_abort = False
                                fail = True
                    if fail:
                        entry['status'] = 503
                        rule.failed.set()
                        if not rule.failure_release.wait(45):
                            raise TimeoutError('abort gate timed out')
                        data = b'<Error><Code>SlowDown</Code><Message>Temporary outage</Message></Error>'
                        status, headers = 503, [('Content-Type', 'application/xml')]
                    else:
                        connection = http.client.HTTPConnection(owner.upstream.hostname,
                                                               owner.upstream.port, timeout=30)
                        headers_in = {k: v for k, v in self.headers.items()
                                      if k.lower() not in ('host', 'connection', 'transfer-encoding')}
                        headers_in['Host'] = owner.upstream.netloc
                        connection.request(self.command, self.path, body, headers_in)
                        response = connection.getresponse()
                        status, headers, data = response.status, response.getheaders(), response.read()
                        connection.close()
                        entry.update(status=status, forwarded=True)
                        if op == 'complete':
                            entry['response'] = data.decode(errors='replace')
                    if query_rule:
                        query_rule.arrived.set()
                        if not query_rule.release.wait(45):
                            raise TimeoutError('query gate timed out')
                    if rule:
                        chosen = False
                        with owner.lock:
                            if not rule.used and ((rule.mode == 'commit' and op in ('complete', 'publish'))
                                                  or (rule.mode == 'create' and op == 'create')
                                                  or (rule.mode == 'part' and op == 'part')):
                                rule.used = True
                                chosen = True
                        if chosen:
                            rule.arrived.set()
                            if not rule.release.wait(45):
                                raise TimeoutError('response gate timed out')
                            if rule.mode == 'create':
                                if hung_up(self.connection):
                                    found = re.search(rb'<UploadId>([^<]+)</UploadId>', data)
                                    rule.unanswered = found.group(1).decode() if found else ''
                                rule.replied.set()
                            if rule.mode in ('commit', 'part'):
                                self.close_connection = True
                                return
                        if rule.mode == 'stall' and op in ('complete', 'publish'):
                            time.sleep(rule.hold)
                    self.send_response(status)
                    for k, v in headers:
                        if k.lower() not in ('content-length', 'transfer-encoding', 'connection', 'server', 'date'):
                            self.send_header(k, v)
                    length = next((v for k, v in headers if k.lower() == 'content-length'), '0') if self.command == 'HEAD' else str(len(data))
                    self.send_header('Content-Length', length)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(data)
                    self.close_connection = True
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    entry['error'] = repr(error)
                    self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = 'http://127.0.0.1:' + str(self.server.server_port)

    def arm(self, key, mode=None, fail_abort=False):
        rule = Rule(mode, fail_abort)
        self.rules[key] = rule
        return rule

    def close(self):
        for rule in self.rules.values():
            rule.release.set()
            rule.failure_release.set()
        if self.query_rule:
            self.query_rule.release.set()
        self.server.shutdown()
        self.server.server_close()
