"""Independent HTTP gateway: one response-loss window, no SDK mocks."""
import http.server
import json
import socket
import threading
import urllib.error
import urllib.request


class UncertainLink:
    def __init__(self, upstream, table, committed):
        self.upstream, self.table, self.committed = upstream, table, committed
        self.armed = threading.Event()
        self.pending = threading.Event()
        self.release = threading.Event()
        self.applied = threading.Event()
        self.deliver = threading.Event()
        self.lock = threading.Lock()
        self.count = 0
        self.trace = []
        self.errors = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                target = self.headers.get('X-Amz-Target', '').split('.')[-1]
                payload = json.loads(body)
                tables = []
                if target in ('PutItem', 'UpdateItem', 'DeleteItem'):
                    tables = [payload.get('TableName')]
                elif target == 'TransactWriteItems':
                    tables = [entry[kind].get('TableName')
                              for entry in payload.get('TransactItems', [])
                              for kind in ('Put', 'Update', 'Delete') if kind in entry]
                elif target == 'BatchWriteItem':
                    tables = list(payload.get('RequestItems', {}))
                elif target in ('ExecuteStatement', 'ExecuteTransaction', 'BatchExecuteStatement'):
                    if owner.table in json.dumps(payload):
                        tables = [owner.table]
                number = 0
                with owner.lock:
                    if owner.armed.is_set() and owner.table in tables:
                        owner.count += 1
                        number = owner.count
                        owner.trace.append({'number': number, 'target': target, 'request': payload})
                try:
                    if number == 1:
                        owner.pending.set()
                        if not owner.release.wait(15):
                            raise TimeoutError('first request release')
                    # The first request may commit. During the rest of the outage
                    # requests are rejected before forwarding. After two errors
                    # the gateway stays healthy, including all consistent reads.
                    if number not in (1, 2) or (number == 1 and owner.committed):
                        headers = {k: v for k, v in self.headers.items()
                                   if k.lower() not in ('host', 'content-length', 'connection')}
                        req = urllib.request.Request(owner.upstream, data=body, headers=headers, method='POST')
                        try:
                            response = urllib.request.urlopen(req, timeout=10)
                        except urllib.error.HTTPError as exc:
                            response = exc
                        with response:
                            status, data, response_headers = response.status, response.read(), dict(response.headers)
                    else:
                        status, data, response_headers = None, None, {}
                    if number == 1:
                        owner.trace[0]['upstream_status'] = status
                        owner.applied.set()
                        if not owner.deliver.wait(15):
                            raise TimeoutError('first response release')
                    if number in (1, 2):
                        status = 500
                        data = b'{"__type":"com.amazonaws.dynamodb.v20120810#InternalServerError","message":"gateway upstream outcome unavailable"}'
                        response_headers = {'Content-Type': 'application/x-amz-json-1.0'}
                    self.send_response(status)
                    for key, value in response_headers.items():
                        if key.lower() not in ('content-length', 'connection', 'transfer-encoding', 'server', 'date'):
                            self.send_header(key, value)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    # A client may abandon a failed connection and reconcile by read.
                    self.close_connection = True
                except Exception as exc:
                    owner.errors.append(repr(exc))
                    self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self):
        return 'http://127.0.0.1:' + str(self.server.server_address[1])

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release.set()
        self.deliver.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
