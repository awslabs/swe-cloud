"""Output comparison helpers shared by the test modules."""


def lines(data: bytes):
    """Split newline-terminated output; the final record must be terminated."""
    assert data == b"" or data.endswith(b"\n"), data
    return data.split(b"\n")[:-1] if data else []


def by_partition(rows, partition_of):
    """Group output rows per partition, keeping their order inside each partition."""
    grouped = {}
    for row in rows:
        grouped.setdefault(partition_of(row), []).append(row)
    return grouped


def same_json(actual, expected, path="$"):
    """Decoded-JSON equality that keeps strings, numbers, booleans and null distinct.

    Numbers compare by value (1, 1.0 and 1e0 are equivalent); booleans are never numbers.
    """
    if expected is None or isinstance(expected, bool):
        assert actual is expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, (int, float)):
        assert type(actual) in (int, float) and actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, str):
        assert type(actual) is str and actual == expected, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, list):
        assert type(actual) is list and len(actual) == len(expected), f"{path}: {actual!r} != {expected!r}"
        for i, (a, e) in enumerate(zip(actual, expected)):
            same_json(a, e, f"{path}[{i}]")
    else:
        assert type(actual) is dict and set(actual) == set(expected), f"{path}: keys {sorted(actual) if isinstance(actual, dict) else actual!r} != {sorted(expected)}"
        for key in expected:
            same_json(actual[key], expected[key], f"{path}.{key}")
