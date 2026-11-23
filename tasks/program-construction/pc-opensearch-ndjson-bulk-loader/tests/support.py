"""Shared helpers: run the loader against an index and inspect OpenSearch independently.

Numbers in stored documents follow the public value policy: they are decoded as
Decimal (1, 1.0 and 1e0 are equal) while booleans, strings and null stay distinct.
"""

import json
import re
import urllib.error
import urllib.request
from decimal import Decimal

from harness import run_program, write_fixture

LOG_PREFIX = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")


def decode(text):
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)


def norm(value):
    """Expected values written as Python literals, decoded like observed ones."""
    return decode(json.dumps(value))


def canon(value):
    """A comparable form of decoded JSON: numbers by value, booleans kept apart from numbers."""
    if isinstance(value, bool) or value is None:
        return ("literal", value)
    if isinstance(value, Decimal):
        return ("number", str(value.normalize()))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("list", tuple(canon(v) for v in value))
    if isinstance(value, dict):
        return ("object", tuple(sorted((k, canon(v)) for k, v in value.items())))
    raise TypeError(type(value))


def same(observed, expected):
    return canon(observed) == canon(expected)


def http(server, method, path, body=None):
    """Return (status, decoded body) for a raw OpenSearch request."""
    data = None
    if body is not None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
    request = urllib.request.Request(server + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode()
            return response.status, (decode(text) if text else None)
    except urllib.error.HTTPError as error:
        text = error.read().decode()
        try:
            return error.code, decode(text)
        except ValueError:
            return error.code, text


def ok(server, method, path, body=None):
    status, result = http(server, method, path, body)
    assert status < 300, (method, path, status, result)
    return result


def exists(server, index):
    return http(server, "GET", f"/{index}")[0] == 200


def documents(server, index):
    """Map of _id to decoded _source for every document in the index."""
    ok(server, "POST", f"/{index}/_refresh")
    result = ok(server, "POST", f"/{index}/_search", {"size": 10000, "query": {"match_all": {}}})
    hits = result["hits"]["hits"]
    found = {}
    for hit in hits:
        assert hit["_id"] not in found
        found[hit["_id"]] = hit["_source"]
    assert len(found) == int(result["hits"]["total"]["value"])
    return found


def assert_documents(server, index, expected):
    observed = documents(server, index)
    assert sorted(observed) == sorted(expected), observed
    for key, source in expected.items():
        assert same(observed[key], norm(source)), (key, observed[key], source)


def sources(server, index):
    """Sources of an index whose ids were auto-generated, as a sorted multiset."""
    observed = documents(server, index)
    for key in observed:
        assert re.fullmatch(r"[A-Za-z0-9_-]{20}", key), key
    return sorted(canon(s) for s in observed.values())


def expected_sources(*items):
    return sorted(canon(norm(s)) for s in items)


def index_settings(server, index):
    return ok(server, "GET", f"/{index}/_settings")[index]["settings"]["index"]


def properties(server, index):
    return ok(server, "GET", f"/{index}/_mapping")[index]["mappings"].get("properties", {})


def write_lines(root, name, lines, *, newline_at_end=True):
    text = "\n".join(lines) + ("\n" if newline_at_end else "")
    path = root / name
    write_fixture(path, text, mode=0o644)
    return path


def ndjson(*docs):
    return [json.dumps(d) for d in docs]


def load(server, index, *flags, path=None, stdin=None, cwd=None, text=True, timeout=45):
    """Run the loader; FILE is given as a name relative to its directory, which becomes the cwd."""
    args = ["-server", server, "-index", index, *flags]
    if path is not None:
        args.append(path.name)
        cwd = cwd or path.parent
    return run_program(args, stdin=stdin, timeout=timeout, cwd=str(cwd) if cwd else None, text=text)


BULK_FAILED = (
    "error during bulk operation, check error details; maybe try fewer workers (-w) "
    "or increase thread_pool.bulk.queue_size in your nodes"
)
WORKER0 = "worker worker-0: worker bulk index operation failed: "


def worker_errors(*errors):
    """The final message of a single-worker run whose batches failed in this order."""
    return "worker errors occurred: " + "; ".join(WORKER0 + e for e in errors)


def final(stderr):
    lines = messages(stderr)
    return lines[-1] if lines else ""


def messages(stderr):
    """Stderr with the log timestamp removed from each line."""
    return [LOG_PREFIX.sub("", line) for line in stderr.splitlines()]
