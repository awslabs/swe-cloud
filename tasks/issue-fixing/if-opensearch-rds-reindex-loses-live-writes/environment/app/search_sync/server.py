"""The after-commit hook: the catalog app calls POST /on_commit after every committed product change.

Request body (see catalog/writes.py):

    {"id": 17, "op": "upsert", "row": {"id": 17, "name": ..., "price_cents": ..., "brand": ..., "updated_at": ...}}
    {"id": 42, "op": "delete", "row": null}

The row is the state the app just committed. Answer 2xx once search reflects it.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, documents
from .opensearch import OpenSearch


class Sync:
    def __init__(self) -> None:
        self.search = OpenSearch(config.opensearch_url())
        self.alias = config.ALIAS

    def on_commit(self, change: dict) -> None:
        doc_id = documents.doc_id(change["id"])
        if change["op"] == "upsert":
            self.search.index_doc(self.alias, doc_id, documents.document(change["row"]))
        elif change["op"] == "delete":
            self.search.delete_doc(self.alias, doc_id)
        else:
            raise ValueError(f"unknown op {change['op']!r}")


class Handler(BaseHTTPRequestHandler):
    sync: Sync

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/on_commit":
            self._reply(404, {"error": "not found"})
            return
        try:
            change = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self.sync.on_commit(change)
        except Exception as error:  # the app logs a failed hook; tell it what went wrong
            traceback.print_exc(file=sys.stderr)
            self._reply(500, {"error": f"{type(error).__name__}: {error}"})
            return
        self._reply(200, {"ok": True})

    def log_message(self, fmt: str, *args) -> None:  # quiet: one line per request is too much at volume
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Many app processes call the hook at once; the socketserver default listen backlog (5) drops
    # connections in a burst, and a dropped SYN costs the caller a 1 s retransmit.
    request_queue_size = 256


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="search sync after-commit hook server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8702)
    args = parser.parse_args(argv)
    Handler.sync = Sync()
    server = Server((args.host, args.port), Handler)
    print(f"sync-server listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
