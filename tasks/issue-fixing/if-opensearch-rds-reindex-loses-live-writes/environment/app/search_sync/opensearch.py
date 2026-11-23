"""A small OpenSearch REST client: just the calls the sync service makes."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class OpenSearchError(RuntimeError):
    def __init__(self, status: int, body: object):
        super().__init__(f"OpenSearch answered {status}: {json.dumps(body)[:500]}")
        self.status = status
        self.body = body


class OpenSearch:
    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: object = None, params: dict | None = None,
                ndjson: str | None = None, allow: tuple[int, ...] = ()) -> tuple[int, dict]:
        """Send one request; return (status, body). Statuses outside 2xx raise unless listed in ``allow``."""
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data, ctype = None, "application/json"
        if ndjson is not None:
            data, ctype = ndjson.encode(), "application/x-ndjson"
        elif body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read() or b"{}")
            if error.code in allow:
                return error.code, payload
            raise OpenSearchError(error.code, payload) from None

    # documents ------------------------------------------------------------------------

    def index_doc(self, index: str, doc_id: str, doc: dict, **params) -> dict:
        return self.request("PUT", f"/{index}/_doc/{doc_id}", doc, params=params or None)[1]

    def delete_doc(self, index: str, doc_id: str, **params) -> dict:
        """Delete one document; a document that is not there is not an error."""
        return self.request("DELETE", f"/{index}/_doc/{doc_id}", params=params or None, allow=(404,))[1]

    def get_doc(self, index: str, doc_id: str) -> dict | None:
        status, body = self.request("GET", f"/{index}/_doc/{doc_id}", allow=(404,))
        return body if status == 200 and body.get("found") else None

    def bulk(self, actions: list[tuple[dict, dict | None]]) -> dict:
        """POST /_bulk with (action, source) pairs; source is None for deletes. Returns the response."""
        lines = []
        for action, source in actions:
            lines.append(json.dumps(action))
            if source is not None:
                lines.append(json.dumps(source))
        return self.request("POST", "/_bulk", ndjson="\n".join(lines) + "\n")[1]

    # indices and aliases --------------------------------------------------------------

    def create_index(self, name: str, body: dict) -> None:
        self.request("PUT", f"/{name}", body)

    def refresh(self, name: str) -> None:
        self.request("POST", f"/{name}/_refresh")

    def alias_indices(self, alias: str) -> list[str]:
        """The concrete indices the alias names today (empty if the alias does not exist)."""
        status, body = self.request("GET", f"/_alias/{alias}", allow=(404,))
        return sorted(body) if status == 200 else []

    def swap_alias(self, alias: str, new_index: str) -> None:
        """Point the alias at new_index alone, in one atomic _aliases request."""
        actions = [{"remove": {"index": old, "alias": alias}} for old in self.alias_indices(alias) if old != new_index]
        actions.append({"add": {"index": new_index, "alias": alias}})
        self.request("POST", "/_aliases", {"actions": actions})
