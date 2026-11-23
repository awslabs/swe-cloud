"""Client for the catalog export API, the indexer's only read path to the catalog (see catalog/export_api.py)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

PAGE_LIMIT_MAX = 250


class CatalogExport:
    def __init__(self, url: str, timeout: float = 600.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None):
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def page(self, after_id: int, limit: int = PAGE_LIMIT_MAX) -> list[dict]:
        """Rows with id > after_id, ascending, at most ``limit``."""
        return self._get("/rows", {"after_id": after_id, "limit": min(limit, PAGE_LIMIT_MAX)})["rows"]

    def row(self, product_id: int) -> dict | None:
        """The current row, or None if the product does not exist."""
        return self._get(f"/row/{int(product_id)}")

    def pages(self, limit: int = PAGE_LIMIT_MAX, after_id: int = 0) -> Iterator[list[dict]]:
        """Keyset pagination over the whole table, until a page comes back empty."""
        while True:
            rows = self.page(after_id, limit)
            if not rows:
                return
            yield rows
            after_id = rows[-1]["id"]
