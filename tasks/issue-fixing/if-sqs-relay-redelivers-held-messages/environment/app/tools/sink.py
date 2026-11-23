"""A stand-in downstream for local runs: answers every POST with 200 after --hold seconds.

Like the real downstream it takes one call at a time per relay (the "relay" field of the payload) and
answers 429 to a second concurrent call from the same relay. Each call is printed as one JSON line.

    python3 /app/tools/sink.py --port 8700 --hold 0.5
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--hold", type=float, default=0.5, help="seconds before answering each call")
    args = parser.parse_args()

    in_flight = set()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            relay = payload.get("relay")
            with lock:
                busy = relay in in_flight
                if not busy:
                    in_flight.add(relay)
            begin = time.time()
            if busy:
                self._answer(429)
            else:
                try:
                    time.sleep(args.hold)
                    self._answer(200)
                finally:
                    with lock:
                        in_flight.discard(relay)
            print(json.dumps({"relay": relay, "message_id": payload.get("message_id"), "begin": begin,
                              "end": time.time(), "status": 429 if busy else 200}), flush=True)

        def _answer(self, status):
            body = b"{}"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"sink listening on http://127.0.0.1:{args.port}/ (hold {args.hold} s)", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
