"""HTTP transport used by the verifier; it imports no application code."""
import http.client
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Relay:
    def __init__(self, endpoint, bucket, writer_label, before=None, after=None):
        self.endpoint = urllib.parse.urlsplit(endpoint)
        self.bucket = bucket
        self.writer_label = writer_label
        self.before = before
        self.after = after
        self.stopped = threading.Event()
        self.events = []
        self.errors = []
        self.touched = []
        self.lock = threading.Lock()
        self.dispatch_lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_request(self):
                # Serialize at the gateway so a held request also parks other
                # connections from this worker. This makes state observations
                # at crash/replacement boundaries valid for parallel clients.
                with relay.dispatch_lock:
                    self.forward_request()

            def forward_request(self):
                if relay.stopped.is_set():
                    self.close_connection = True
                    return
                if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
                    chunks = []
                    while True:
                        size = int(self.rfile.readline().split(b';', 1)[0].strip(), 16)
                        if not size:
                            while self.rfile.readline() not in (b'\r\n', b'\n', b''):
                                pass
                            break
                        chunks.append(self.rfile.read(size))
                        if self.rfile.read(2) != b'\r\n':
                            raise ValueError('invalid HTTP chunk terminator')
                    body = b''.join(chunks)
                else:
                    body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                with relay.lock:
                    index = len(relay.events) + 1
                    relay.events.append({'index': index, 'method': self.command,
                                         'path': self.path})
                try:
                    if relay.before:
                        relay.before(index)
                    parsed = urllib.parse.urlsplit(self.path)
                    path = urllib.parse.unquote(parsed.path).lstrip('/')
                    if path.startswith(relay.bucket + '/'):
                        key = path[len(relay.bucket) + 1:]
                    else:
                        key = path  # virtual-host addressing
                    protected = f'backup/{relay.writer_label}/'
                    if self.command in ('DELETE', 'PUT', 'POST', 'PATCH'):
                        if key.startswith(protected):
                            relay.touched.append((self.command, key))
                        if self.command == 'POST' and 'delete' in urllib.parse.parse_qs(
                                parsed.query, keep_blank_values=True):
                            root = ET.fromstring(body)
                            for node in root.iter():
                                if node.tag.rsplit('}', 1)[-1] == 'Key' and (node.text or '').startswith(protected):
                                    relay.touched.append(('DELETE', node.text))
                    conn = http.client.HTTPConnection(relay.endpoint.hostname,
                                                      relay.endpoint.port, timeout=25)
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in ('host', 'connection', 'accept-encoding', 'transfer-encoding')}
                    headers['Host'] = relay.endpoint.netloc
                    headers['Connection'] = 'close'
                    if body or 'Content-Length' in self.headers:
                        headers['Content-Length'] = str(len(body))
                    # Convert a virtual-host URL to the equivalent path-style route.
                    forward_path = self.path
                    if not path.startswith(relay.bucket + '/') and path != relay.bucket:
                        host = self.headers.get('Host', '').split(':')[0]
                        if host.startswith(relay.bucket + '.'):
                            forward_path = '/' + relay.bucket + self.path
                    conn.request(self.command, forward_path, body=body, headers=headers)
                    response = conn.getresponse()
                    payload = response.read()
                    relay.events[index - 1]['status'] = response.status
                    if relay.after and relay.after(index) is False:
                        relay.stopped.set()
                        conn.close()
                        self.close_connection = True
                        return
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in ('connection', 'transfer-encoding', 'content-length', 'server', 'date'):
                            self.send_header(name, value)
                    length = response.getheader('Content-Length', '0') if self.command == 'HEAD' else str(len(payload))
                    self.send_header('Content-Length', length)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(payload)
                    self.close_connection = True
                    conn.close()
                except (BrokenPipeError, ConnectionResetError) as exc:
                    if not relay.stopped.is_set():
                        relay.errors.append(repr(exc))
                    self.close_connection = True
                except Exception as exc:
                    relay.errors.append(repr(exc))
                    self.send_error(502, 'verifier transport error')
                    self.close_connection = True

            do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
