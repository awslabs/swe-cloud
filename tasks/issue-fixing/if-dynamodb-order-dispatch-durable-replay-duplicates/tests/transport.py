"""A transparent DynamoDB link with one controllable in-flight mutation."""
import http.server
import json
import socket
import threading
import urllib.error
import urllib.request


class Link:
    def __init__(self, upstream, table, hold_all=False):
        self.upstream = upstream
        self.table = table
        self.hold_all = hold_all
        self.armed = threading.Event()
        self.pending = threading.Event()
        self.release = threading.Event()
        self.applied = threading.Event()
        self.deliver = threading.Event()
        self.claimed = False
        self.lock = threading.Lock()
        self.observation = None
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
                writes = []
                if target in ('PutItem', 'UpdateItem', 'DeleteItem'):
                    writes = [payload.get('TableName')]
                elif target == 'TransactWriteItems':
                    writes = [item.get(kind, {}).get('TableName')
                              for item in payload.get('TransactItems', [])
                              for kind in ('Put', 'Update', 'Delete') if kind in item]
                elif target == 'BatchWriteItem':
                    writes = list(payload.get('RequestItems', {}))
                elif target in ('ExecuteStatement', 'ExecuteTransaction', 'BatchExecuteStatement'):
                    # PartiQL statements use SQL table identifiers rather than TableName.
                    if owner.table in json.dumps(payload):
                        writes = [owner.table]
                hold = False
                with owner.lock:
                    if owner.armed.is_set() and (not owner.claimed or owner.hold_all) and owner.table in writes:
                        owner.claimed = True
                        owner.observation = {'target': target, 'request': payload}
                        hold = True
                if hold:
                    owner.pending.set()
                    if not owner.release.wait(20):
                        owner.errors.append('driver did not release in-flight request')
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ('host', 'content-length', 'connection')}
                try:
                    request = urllib.request.Request(owner.upstream, data=body, headers=headers, method='POST')
                    try:
                        response = urllib.request.urlopen(request, timeout=10)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        data = response.read()
                        if hold:
                            # Observe durable AWS state before the worker sees its
                            # response and can try to compensate for a stale write.
                            owner.applied.set()
                            if not owner.deliver.wait(20):
                                owner.errors.append('driver did not deliver AWS response')
                        self.send_response(response.status)
                        for key, value in response.headers.items():
                            if key.lower() not in ('content-length', 'connection', 'transfer-encoding', 'server', 'date'):
                                self.send_header(key, value)
                        self.send_header('Content-Length', str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
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
