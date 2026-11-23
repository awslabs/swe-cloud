"""Independent enrichment endpoint and receipt observer used by the deployment load."""
import base64
import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Enrichment:
    def __init__(self, violations):
        self.violations = violations
        self.lock = threading.Lock()
        self.active, self.responses = {}, {}
        self.accepted = set()
        self.slow = False
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                key = request['topic'], request['partition'], request['offset']
                token = uuid.uuid4().hex
                with outer.lock:
                    for other in outer.active.values():
                        if (other['topic'], other['partition']) == key[:2] and other['instance_id'] != request['instance_id']:
                            outer.violations.append(f'concurrent enrichment by different owners: {key[:2]}')
                    outer.active[token] = request
                    outer.accepted.add(key)
                # Ordinary variable remote latency: an early offset can finish last.
                if outer.slow: time.sleep(4 if key[2] % 12 in (0, 1, 5) else 0.05)
                result = dict(receipt=token, sha256=hashlib.sha256(base64.b64decode(request['value'])).hexdigest())
                with outer.lock:
                    outer.responses[token] = (request, result)
                    del outer.active[token]
                try:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps(result).encode())
                except (BrokenPipeError, ConnectionResetError): pass
        class Server(ThreadingHTTPServer): request_queue_size = 128
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def handoff(self, facts):
        with self.lock:
            active = [r for r in self.active.values() if r['instance_id']==facts['instance_id']
                      and r['topic']==facts['topic'] and r['partition'] in facts['partitions']]
        if active: self.violations.append(f'revocation announced with {len(active)} enrichment requests active')

    def busy(self, instance):
        with self.lock: return any(r['instance_id']==instance for r in self.active.values())

    def validate(self, value):
        receipt = json.loads(value)
        token = receipt['result']['receipt']
        with self.lock: issued = self.responses.get(token)
        if not issued: raise AssertionError(f'output contains an unissued service receipt: {token}')
        request, result = issued
        if receipt['result'] != result or any(receipt.get(k) != request[k] for k in
                ('topic', 'partition', 'offset', 'value', 'instance_id')):
            raise AssertionError('receipt does not match enrichment response')
        return (request['topic'], request['partition'], request['offset']), base64.b64decode(request['value'])

    def close(self):
        self.server.shutdown(); self.server.server_close()
