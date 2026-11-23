"""How the storefront app changes a product: a reference copy of the app's catalog write path.

Each change to a product is one transaction. The app never has two changes to the same product in
flight: it holds the product's row (SELECT ... FOR UPDATE in the app; a per-product lock here) from
BEGIN until the search hook has answered, so changes to one product reach the hook one at a time,
in commit order. Changes to different products run concurrently from many app processes.

After COMMIT the app POSTs the change to the search sync hook and waits at most HOOK_TIMEOUT_S. A
hook that errors or times out is logged and dropped: the app does not retry, so that change reaches
search only with the product's next change or the next full reindex.

``updated_at`` is stamped from the wall clock of the app host making the change. The storefront runs
on several app hosts whose clocks are only loosely synchronised (seconds apart has been seen), so
``updated_at`` is for display; it does not order changes.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import urllib.request
from collections import defaultdict

HOOK_TIMEOUT_S = 2.0
log = logging.getLogger("catalog.writes")
_locks: defaultdict[int, threading.Lock] = defaultdict(threading.Lock)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def row_json(row: dict) -> dict:
    return {**row, "updated_at": row["updated_at"].isoformat()}


def notify(hook_url: str, change: dict) -> bool:
    """POST one committed change to the search hook. Failures are logged, never raised or retried."""
    request = urllib.request.Request(hook_url, data=json.dumps(change).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=HOOK_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except Exception as error:
        log.warning("search hook failed for product %s: %s", change["id"], error)
        return False


COLUMNS = "id, name, price_cents, brand, updated_at"


def _row(cur) -> dict | None:
    found = cur.fetchone()
    return dict(zip(("id", "name", "price_cents", "brand", "updated_at"), found)) if found else None


def insert_product(conn, hook_url: str, name: str, price_cents: int, brand: str) -> int:
    with conn.transaction():
        row = _row(conn.execute(f"INSERT INTO products (name, price_cents, brand, updated_at) VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}",
                                (name, price_cents, brand, now())))
    with _locks[row["id"]]:
        notify(hook_url, {"id": row["id"], "op": "upsert", "row": row_json(row)})
    return row["id"]


def update_product(conn, hook_url: str, product_id: int, **fields) -> bool:
    with _locks[product_id]:
        sets = ", ".join(f"{column} = %s" for column in fields) + ", updated_at = %s"
        with conn.transaction():
            row = _row(conn.execute(f"UPDATE products SET {sets} WHERE id = %s RETURNING {COLUMNS}",
                                    (*fields.values(), now(), product_id)))
        if row is None:
            return False
        notify(hook_url, {"id": product_id, "op": "upsert", "row": row_json(row)})
        return True


def delete_product(conn, hook_url: str, product_id: int) -> bool:
    with _locks[product_id]:
        with conn.transaction():
            gone = conn.execute("DELETE FROM products WHERE id = %s", (product_id,)).rowcount
        if not gone:
            return False
        notify(hook_url, {"id": product_id, "op": "delete", "row": None})
        return True
