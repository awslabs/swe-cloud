"""Verifier-owned HTTP transport; no imports from the application."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from urllib.parse import urlsplit, unquote


def millis():
    return int(time.time() * 1000)


class Relay:
    def __init__(self, timeline, serial, backend, mode=None, pace=0, manifest_pace=0):
        self.timeline, self.serial = timeline, serial
        self.backend = urlsplit(backend)
        self.mode, self.pace, self.manifest_pace = mode, pace, manifest_pace
        self.triggered = threading.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.count = 0
        self.fired = False
        self.active = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                key = unquote(urlsplit(self.path).path).split('/', 2)[-1]
                is_record = key.endswith('/.lock')
                data_put = self.command == 'PUT' and '/data/' in key
                record = None
                if is_record and self.command == 'PUT':
                    try:
                        record = json.loads(body)
                    except (ValueError, TypeError):
                        pass
                item = dict(received_ms=millis(), method=self.command, key=key,
                            body=record, if_match=self.headers.get('If-Match'))
                drop = False
                with outer.guard:
                    if data_put:
                        outer.count += 1
                    target = ((outer.mode == 'publish' and record and record.get('state') == 'committed')
                              or (outer.mode == 'upload' and data_put and outer.count == 2)
                              or (outer.mode == 'claim' and record and record.get('state') == 'held'))
                    if target and not outer.fired:
                        outer.fired = True
                        outer.active = True
                        outer.triggered.set()
                    if outer.mode in ('drop_publish', 'drop_claim') and not outer.fired and record:
                        wanted = 'committed' if outer.mode == 'drop_publish' else 'held'
                        if record.get('state') == wanted:
                            outer.fired = drop = True
                    wait = outer.active
                if wait and not outer.release.wait(35):
                    self.send_error(504, 'driver did not release request')
                    return
                if data_put and outer.pace:
                    time.sleep(outer.pace)
                if outer.manifest_pace and self.command == 'PUT' and '/manifests/' in key:
                    time.sleep(outer.manifest_pace)
                # Ordering only individual catalog operations makes the historical
                # log unambiguous; it does not combine a worker's GET and PUT.
                lock = outer.serial if is_record else threading.Lock()
                recorded = False
                try:
                    with lock:
                        item['sent_ms'] = millis()
                        connection = http.client.HTTPConnection(outer.backend.hostname,
                                                               outer.backend.port, timeout=25)
                        headers = dict(self.headers)
                        headers['Connection'] = 'close'
                        connection.request(self.command, self.path, body=body, headers=headers)
                        response = connection.getresponse()
                        payload = response.read()
                        item.update(status=response.status, etag=response.getheader('ETag'),
                                    finished_ms=millis(), dropped=drop)
                        if is_record and self.command == 'GET' and response.status == 200:
                            item['observed'] = json.loads(payload)
                        outer.timeline.append(item)
                        recorded = True
                        reply_headers = response.getheaders()
                        connection.close()
                    if drop:
                        outer.triggered.set()
                        self.close_connection = True
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                    self.send_response(item['status'])
                    for key, value in reply_headers:
                        if key.lower() not in ('connection', 'transfer-encoding', 'content-length', 'server', 'date'):
                            self.send_header(key, value)
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(payload)
                    self.close_connection = True
                except (OSError, http.client.HTTPException) as exc:
                    if recorded:
                        item['delivery_error'] = str(exc)
                    else:
                        item.update(status=599, error=str(exc), finished_ms=millis())
                        outer.timeline.append(item)
                    self.close_connection = True

            do_GET = do_PUT = do_DELETE = do_POST = do_HEAD = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
