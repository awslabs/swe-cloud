"""The catalog export API: the read path the search indexer uses (a reference copy; owned by the catalog team).

    GET /rows?after_id=<int>&limit=<int <= 250>   -> {"rows": [row, ...]}  ids > after_id, ascending
    GET /row/<id>                                 -> row, or 404 if the product does not exist

A row is {"id", "name", "price_cents", "brand", "updated_at"}. Every request runs one query on its
own connection and returns what that query saw. Nothing is held between requests (no snapshot, no
cursor), so consecutive pages are not a consistent view of the table, and a row may change at any
moment after the query that returned it.

In production this is served from the catalog's read replica behind the storefront's load
balancer. Under load an export request has been observed to stall for well over a minute between
its query and its response.

Run it with ``python3 -m catalog.export_api --port 8701`` and CATALOG_DSN set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

PAGE_LIMIT_MAX = 250
COLUMNS = ("id", "name", "price_cents", "brand", "updated_at")


def serialize(found) -> dict:
    row = dict(zip(COLUMNS, found))
    row["updated_at"] = row["updated_at"].isoformat()
    return row


class Handler(BaseHTTPRequestHandler):
    dsn: str

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/rows":
                after_id = int(query.get("after_id", ["0"])[0])
                limit = int(query.get("limit", [str(PAGE_LIMIT_MAX)])[0])
                if not 0 < limit <= PAGE_LIMIT_MAX:
                    self._reply(400, {"error": f"limit must be 1..{PAGE_LIMIT_MAX}"})
                    return
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM products WHERE id > %s ORDER BY id LIMIT %s",
                                        (after_id, limit)).fetchall()
                self._reply(200, {"rows": [serialize(r) for r in rows]})
            elif url.path.startswith("/row/"):
                product_id = int(url.path.rsplit("/", 1)[1])
                with psycopg.connect(self.dsn, autocommit=True) as conn:
                    found = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM products WHERE id = %s", (product_id,)).fetchone()
                if found is None:
                    self._reply(404, {"error": "no such product"})
                else:
                    self._reply(200, serialize(found))
            else:
                self._reply(404, {"error": "not found"})
        except ValueError as error:
            self._reply(400, {"error": str(error)})

    def log_message(self, fmt: str, *args) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="catalog export API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8701)
    args = parser.parse_args(argv)
    Handler.dsn = os.environ["CATALOG_DSN"]
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"export API listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
