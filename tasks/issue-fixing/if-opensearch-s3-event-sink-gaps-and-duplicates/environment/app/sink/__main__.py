import json
import logging
import queue
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .bulk import BulkWorker
from .inbox import Inbox
from .query import QueryManager
from .transport import request


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s')
    stop = threading.Event()
    incoming, ready = queue.Queue(), queue.Queue()
    inbox = Inbox()
    queries = QueryManager(ready, inbox)
    bulk = BulkWorker(incoming, ready, queries, inbox)
    for record in inbox.all():
        if record['phase'] != 'done':
            incoming.put(record['event'])

    class Intake(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status):
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_GET(self):
            if self.path != '/healthz':
                return self.respond(404)
            try:
                response = request('GET', '/')
                self.respond(200 if response.ok and not stop.is_set() else 503)
            except Exception:
                self.respond(503)

        def do_POST(self):
            if self.path != '/events':
                return self.respond(404)
            if stop.is_set():
                return self.respond(503)
            try:
                documents = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                assert isinstance(documents, list)
                assert all(isinstance(doc, dict) and all(key in doc for key in ('key', 'ts', 'payload'))
                           for doc in documents)
            except Exception:
                return self.respond(400)
            try:
                for document in documents:
                    if inbox.accept(document):
                        incoming.put(document)
            except Exception:
                logging.exception('Cannot buffer intake')
                return self.respond(503)
            self.respond(202)

    server = ThreadingHTTPServer(('127.0.0.1', config.INTAKE_PORT), Intake)
    server.daemon_threads = True
    threads = [threading.Thread(target=queries.run, args=(stop,), daemon=True),
               threading.Thread(target=bulk.run, args=(stop,), daemon=True)]
    for thread in threads:
        thread.start()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever(poll_interval=0.1)
    for thread in threads:
        thread.join(timeout=1)
    server.server_close()


if __name__ == '__main__':
    main()
