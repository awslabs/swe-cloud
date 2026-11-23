"""`node serve`: boot from --db-dir and the bucket, then serve the commit API."""
import json
import logging
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .replicator import Replicator, ReuseGeneration

log = logging.getLogger("strata.server")

SHUTDOWN_DRAIN_SECONDS = 12.0


def boot(replicator):
    action, recovered = replicator.restore()
    if isinstance(action, ReuseGeneration):
        replicator.set_generation(action.generation, action.last_frame)
    else:
        replicator.new_generation()
        replicator.snapshot_main_db_file()
    log.info("serving generation %s (recovered=%s)", replicator.generation, recovered)


def make_handler(replicator):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        def _reply(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"generation": replicator.generation,
                                  "seq": replicator.db.seq()})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            try:
                if self.path == "/commit":
                    req = self._body()
                    row_id, payload = int(req["id"]), str(req["payload"])
                    self._reply(200, {"seq": replicator.commit(row_id, payload)})
                elif self.path == "/checkpoint":
                    self._body()
                    old, new = replicator.checkpoint()
                    self._reply(200, {"old": old, "new": new})
                else:
                    self._reply(404, {"error": "not found"})
            except (KeyError, ValueError) as e:
                self._reply(400, {"error": str(e)})
            except Exception as e:
                log.error("%s failed: %s", self.path, e)
                self._reply(503, {"error": str(e)})

    return Handler


def serve(db_dir, bucket, db_name, port):
    replicator = Replicator(db_dir, bucket, db_name)
    boot(replicator)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(replicator))
    server.daemon_threads = True
    stopping = threading.Event()

    def on_sigterm(signum, frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)
    log.info("listening on 127.0.0.1:%d", port)
    server.serve_forever(poll_interval=0.1)
    server.server_close()
    # drain pending uploads before exiting
    with replicator.lock:
        replicator.drain(SHUTDOWN_DRAIN_SECONDS)
    log.info("shut down")
    sys.exit(0)
