"""HTTP ingress for the audit publisher. Run with KAFKA_BOOTSTRAP, TOPIC, PORT."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from delivery import Publisher


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

    def do_GET(self):
        self.reply(200 if self.path == '/healthz' else 404, {'ready': True})

    def do_POST(self):
        if self.path != '/publish': return self.reply(404, {})
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            result = self.server.publisher.publish(data)
            self.reply(200, result)
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {'error': str(e)})


if __name__ == '__main__':
    publisher = Publisher(os.environ['KAFKA_BOOTSTRAP'], os.environ.get('TOPIC', 'ledger'))
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8080'))), Handler)
    server.daemon_threads = True
    server.publisher = publisher
    server.serve_forever()
