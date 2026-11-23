"""Small assertion and encoding helpers shared by the test modules."""

import base64
import json
import urllib.parse


def urlsafe(blob):
    return base64.urlsafe_b64encode(blob).decode()


def query(value):
    return urllib.parse.quote(value, safe="")


def json_stream(text):
    """JSON output: one value per document, documents separated by a `---` line."""
    docs, chunk = [], []
    for line in text.splitlines():
        if line.strip() == "---":
            docs.append(json.loads("\n".join(chunk)))
            chunk = []
        else:
            chunk.append(line)
    if "".join(chunk).strip():
        docs.append(json.loads("\n".join(chunk)))
    return docs


def same(actual, expected, where="$"):
    """Decoded-data equality that keeps strings, booleans, null and numbers distinct.

    Numbers compare by value (1 == 1.0); booleans are never numbers; mappings need
    identical key sets; sequences keep order and length.
    """
    if expected is None or isinstance(expected, (bool, str)):
        assert type(actual) is type(expected) and actual == expected, f"{where}: {actual!r} != {expected!r}"
    elif isinstance(expected, (int, float)):
        assert type(actual) in (int, float) and actual == expected, f"{where}: {actual!r} != {expected!r}"
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{where}: {actual!r} is not a mapping"
        assert set(actual) == set(expected), f"{where}: keys {sorted(actual)} != {sorted(expected)}"
        for key, value in expected.items():
            same(actual[key], value, f"{where}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{where}: {actual!r} != {expected!r}"
        for index, (got, want) in enumerate(zip(actual, expected)):
            same(got, want, f"{where}[{index}]")
    else:
        raise TypeError(f"unsupported expectation {expected!r}")


def ok(proc):
    assert proc.returncode == 0, f"exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return proc


def fails(proc):
    assert proc.returncode != 0, f"unexpected success\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout == "", f"failed run wrote stdout:\n{proc.stdout}"
    return proc
