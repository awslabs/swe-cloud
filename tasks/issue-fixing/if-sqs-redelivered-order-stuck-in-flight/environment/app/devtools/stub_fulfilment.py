#!/usr/bin/env python3
"""Fulfilment stand-in for local runs: accepts every order after a short delay."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        time.sleep(0.2)
        body = json.dumps({"outcome": "success"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print("fulfilled", payload.get("message_id"), payload.get("receive_count"), flush=True)

    def log_message(self, *args):
        pass


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
