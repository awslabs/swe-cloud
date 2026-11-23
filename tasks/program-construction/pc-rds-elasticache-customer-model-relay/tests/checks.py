"""Small comparison helpers implementing the public equivalence policy of /docs/interface.md.

* JSON numbers compare by value (1 == 1.0 == 1e0); booleans are never numbers.
* Token `models` / `subnet`: null and "" are the same "unset" value.
* Free-text messages are not compared.
"""

import json

TOKEN_FIELDS = {"id", "user_id", "key", "status", "name", "created_time", "accessed_time", "expired_time",
                "remain_quota", "unlimited_quota", "used_quota", "models", "subnet"}


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def same(actual, expected, path="$"):
    """Recursive JSON equality: numbers by value, bool distinct from numbers, dict keys exact, lists ordered."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        assert type(actual) is bool and type(expected) is bool and actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif is_number(expected):
        assert is_number(actual) and actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: {actual!r} is not an object"
        assert set(actual) == set(expected), f"{path}: keys {sorted(actual)} != {sorted(expected)}"
        for key in expected:
            same(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), f"{path}: {actual!r} != {expected!r}"
        for index, (left, right) in enumerate(zip(actual, expected)):
            same(left, right, f"{path}[{index}]")
    else:
        assert type(actual) is type(expected) and actual == expected, f"{path}: {actual!r} != {expected!r}"


def unset(value):
    return value in (None, "")


def number(value, expected, what="value"):
    assert is_number(value) and value == expected, f"{what}: {value!r} != {expected!r}"


def check_token(record, expected, created_window=None):
    """A token object from the API: exactly TOKEN_FIELDS; expected values for the listed fields."""
    assert isinstance(record, dict) and set(record) == TOKEN_FIELDS, f"token fields {sorted(record) if isinstance(record, dict) else record!r}"
    for field, want in expected.items():
        if field in ("models", "subnet") and unset(want):
            assert unset(record[field]), f"{field}: {record[field]!r} should be unset"
        else:
            same(record[field], want, field)
    if created_window is not None:
        low, high = created_window
        assert is_number(record["created_time"]) and low <= record["created_time"] <= high, record["created_time"]


def check_row(row, expected):
    """A tokens/users row fetched with psycopg against expected column values."""
    for field, want in expected.items():
        if field in ("models", "subnet") and unset(want):
            assert unset(row[field]), f"row {field}: {row[field]!r} should be unset"
        elif isinstance(want, bool):
            assert row[field] is want, f"row {field}: {row[field]!r} != {want!r}"
        else:
            assert row[field] == want, f"row {field}: {row[field]!r} != {want!r}"


def ok_envelope(status, reply):
    assert status == 200, (status, reply)
    assert isinstance(reply, dict) and reply.get("success") is True, reply
    assert isinstance(reply.get("message"), str), reply
    return reply.get("data")


def failed_envelope(reply, status, http_status=200):
    """A refused management call: the documented envelope with success false (message text is not graded)."""
    assert status == http_status, (status, reply)
    assert isinstance(reply, dict) and reply.get("success") is False and isinstance(reply.get("message"), str), reply
    assert reply.get("message"), "a refusal carries a non-empty message"


def relay_error(status, reply, http_status, error_type="one_api_error", code=None):
    assert status == http_status, (status, reply)
    assert isinstance(reply, dict) and isinstance(reply.get("error"), dict), reply
    assert isinstance(reply["error"].get("message"), str) and reply["error"]["message"], reply
    assert reply["error"].get("type") == error_type, reply
    if code is not None:
        assert reply["error"].get("code") == code, reply


def cached_token(relay, key):
    raw = relay.kv.get(f"token:{key}")
    assert raw is not None, f"token:{key} is not cached"
    return json.loads(raw)


def ttl_within(relay, name, sync):
    ttl = relay.kv.ttl(name)
    assert 0 < ttl <= sync, f"{name} ttl {ttl}"
