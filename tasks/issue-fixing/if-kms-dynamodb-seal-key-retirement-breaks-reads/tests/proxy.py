"""HTTP fault transport. Forward signed headers and body without rewriting them."""
import hashlib
import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WRITE_OPS = {"PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems", "BatchWriteItem",
             "ExecuteStatement", "BatchExecuteStatement", "ExecuteTransaction"}


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


class Pause:
    def __init__(self, body, ids):
        self.body = body
        self.ids = ids
        self.release = threading.Event()
        self.drop = False
        self.status = None
        self.response = None
        self.done = threading.Event()
        self.applied = threading.Event()
        self.after = False


class Proxy:
    def __init__(self, endpoint):
        self.destination = urlparse(endpoint)
        self.held_id = None
        self.hold_response = False
        self.held = None
        self.conflict = None
        self.crash = None
        self.known_ids = set()
        self.excluded_id = None
        self.migration = False
        self.hold_kms = set()
        self.kms_held = []
        self.kms_release = threading.Event()
        self.count = 0
        self.lock = threading.Lock()
        self.events = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                pause = proxy.inspect(self.headers.get("X-Amz-Target", ""), payload)
                if pause and not pause.after:
                    pause.release.wait(100)
                    if pause.drop or not pause.release.is_set():
                        self.close_connection = True
                        pause.done.set()
                        return
                connection = http.client.HTTPConnection(proxy.destination.hostname,
                                                       proxy.destination.port, timeout=95)
                try:
                    headers = {key: value for key, value in self.headers.items()}
                    connection.request("POST", self.path, body=payload, headers=headers)
                    response = connection.getresponse()
                    body = response.read()
                    if pause:
                        pause.status = response.status
                        pause.response = body.decode(errors="replace")
                        pause.applied.set()
                        if pause.after:
                            if 200 <= response.status < 300:
                                pause.release.wait(100)
                                if pause.drop or not pause.release.is_set():
                                    self.close_connection = True
                                    return
                            else:
                                # A failed conditional attempt has no commit to lose.
                                with proxy.lock:
                                    proxy.held = None
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    proxy.events.append({"transport_error": str(error)})
                    self.close_connection = True
                finally:
                    connection.close()
                    if pause:
                        pause.done.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def inspect(self, target, payload):
        if target.startswith("TrentService.") and target.rsplit(".", 1)[-1] in self.hold_kms:
            # Delay every such KMS request of this client, retries included, until released.
            with self.lock:
                if self.kms_release.is_set():
                    return None
                pause = Pause(payload, [])
                pause.release = self.kms_release
                self.kms_held.append(pause)
                self.events.append({"target": target, "delayed": True})
                return pause
        if target.rsplit(".", 1)[-1] not in WRITE_OPS:
            return None
        try:
            body = json.loads(payload)
        except ValueError:
            return None
        values = set(strings(body))
        if "wrapped_dek" not in values or not any("seal_entries" in text for text in values):
            return None
        with self.lock:
            ids = sorted(self.known_ids.intersection(values))
            pause = None
            if self.held_id and self.held_id in values and self.held is None:
                pause = self.held = Pause(payload, [self.held_id])
                pause.after = self.hold_response
            elif self.migration:
                self.count += 1
                candidates = [identity for identity in ids if identity != self.excluded_id]
                if self.conflict is None and candidates:
                    pause = self.conflict = Pause(payload, candidates)
                elif self.crash is None and self.count >= 9:
                    pause = self.crash = Pause(payload, ids)
            if pause:
                self.events.append({"target": target, "ids": ids,
                                    "sha256": hashlib.sha256(payload).hexdigest(),
                                    "body": body})
            return pause

    def close(self):
        for pause in [self.held, self.conflict, self.crash, *self.kms_held]:
            if pause and not pause.release.is_set():
                pause.drop = True
                pause.release.set()
        self.server.shutdown()
        self.server.server_close()
