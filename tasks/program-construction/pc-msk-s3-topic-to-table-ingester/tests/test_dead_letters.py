"""Messages that cannot be ingested: dead-letter records, skipped payloads and their effect on offsets."""

import datetime
import time

from lake import DLQ_FIELDS, b64, check_lock_items, json_value, kinds, quiet, assert_rows_by

BAD_TRAILING = b'{"id": 8, "ts": "2024-03-06"} trailing'
MESSAGES = [
    (0, {"id": 1, "ts": "2024-03-01T10:00:00Z"}),
    (0, b"not json {"),
    (0, b"[1,2]"),
    (0, b"7"),
    (0, None),
    (0, {"id": 6, "ts": 12345}),
    (0, {"id": 7, "ts": "2024-03-05T10:00:00Z"}),
    (0, BAD_TRAILING),
]
# (base64_bytes, json_string) per dead letter, in offset order.
DEAD_LETTERS = [
    (b64(b"not json {"), None),
    (None, "[1,2]"),
    (None, "7"),
    (None, '{"id":6,"ts":12345}'),
    (b64(BAD_TRAILING), None),
]


def dead_letter_matches(row, expected):
    base64_bytes, json_string = expected
    if row["base64_bytes"] != base64_bytes:
        return False
    if json_string is None:
        return row["json_string"] is None
    return isinstance(row["json_string"], str) and json_value(row["json_string"]) == json_value(json_string)


def check_dead_letter_row(row, started, ended):
    assert isinstance(row["error"], str) and row["error"].strip(), row
    stamp = row["timestamp"]
    assert isinstance(stamp, datetime.datetime), row
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    assert started - 5 <= stamp.timestamp() <= ended + 5, (stamp, started, ended)
    return stamp


def test_dead_letters_written_one_commit_each(lake):
    """Adapted from tests/dead_letter_tests.rs (deserialization and transform failures, DLQ date transform)."""
    topic = lake.topic()
    table = lake.table([("id", "integer"), ("ts", "string"), ("date", "string")])
    dlq = lake.table([*DLQ_FIELDS, ("date", "string")], ["date"])
    lake.produce(topic, MESSAGES)
    started = time.time()
    lake.start(lake.args(topic, table, "-t", "date: substr(ts, `0`, `10`)", "--dlq_table_location", dlq.uri,
                         "--dlq_transform", "date: substr(epoch_micros_to_iso8601(timestamp),`0`,`10`)", app="dl"))
    table.wait_version(1)
    dlq.wait_version(5)
    quiet(table, 1, 4)
    ended = time.time()
    assert dlq.versions() == [0, 1, 2, 3, 4, 5]

    # Data: only the two good messages; the offset recorded is the last good one, not the trailing dead letter.
    assert_rows_by(table.rows(), [
        {"id": 1, "ts": "2024-03-01T10:00:00Z", "date": "2024-03-01"},
        {"id": 7, "ts": "2024-03-05T10:00:00Z", "date": "2024-03-05"},
    ], key="id")
    assert table.txns() == {"dl-0": 6}

    # Dead letters: one commit with one file and one row per failed message, in consumption order.
    for version, expected in zip(range(1, 6), DEAD_LETTERS):
        actions = dlq.commit(version)
        assert set(kinds(actions)) - {"commitInfo"} == {"add"} and kinds(actions)["add"] == 1, actions
        dlq.check_adds(version)
        rows = dlq.rows([version])
        assert len(rows) == 1
        row = rows[0]
        assert dead_letter_matches(row, expected), (version, row, expected)
        stamp = check_dead_letter_row(row, started, ended)
        assert row["date"] == stamp.astimezone(datetime.timezone.utc).date().isoformat()
        assert dlq.adds(version)[0]["partitionValues"] == {"date": row["date"]}
    assert dlq.txns() == {}
    check_lock_items(lake, table, [1], started, ended)
    check_lock_items(lake, dlq, [1, 2, 3, 4, 5], started, ended)


def test_without_dead_letter_table_failures_are_dropped(lake):
    topic = lake.topic()
    table = lake.table([("id", "integer"), ("ts", "string"), ("date", "string")])
    lake.produce(topic, MESSAGES)
    lake.start(lake.args(topic, table, "-t", "date: substr(ts, `0`, `10`)", app="drop"))
    table.wait_version(1)
    quiet(table, 1, 4)
    assert_rows_by(table.rows(), [
        {"id": 1, "ts": "2024-03-01T10:00:00Z", "date": "2024-03-01"},
        {"id": 7, "ts": "2024-03-05T10:00:00Z", "date": "2024-03-05"},
    ], key="id")
    assert table.txns() == {"drop-0": 6}


def test_non_object_and_failed_function_messages_without_transforms(lake):
    """Non-object JSON is a dead letter even with no transforms; a function given null fails its transform."""
    topic = lake.topic()
    table = lake.table([("id", "integer"), ("iso", "string")])
    plain = lake.table([("id", "integer"), ("iso", "string")])
    dlq = lake.table(DLQ_FIELDS)
    plain_dlq = lake.table(DLQ_FIELDS)
    lake.produce(topic, [
        (0, {"id": 1, "ms": 0}), (0, b'"just a string"'), (0, {"id": 3}), (0, b"[]"), (0, {"id": 5, "ms": 1000}),
    ])
    started = time.time()
    lake.start(lake.args(topic, table, "-t", "iso: epoch_millis_to_iso8601(ms)", "--dlq_table_location", dlq.uri, app="fn"))
    lake.start(lake.args(topic, plain, "--dlq_table_location", plain_dlq.uri, app="plain"))
    table.wait_version(1)
    plain.wait_version(1)
    quiet(table, 1, 4)
    ended = time.time()

    assert_rows_by(table.rows(), [{"id": 1, "iso": "1970-01-01T00:00:00Z"}, {"id": 5, "iso": "1970-01-01T00:00:01Z"}], key="id")
    assert table.txns() == {"fn-0": 4}
    letters = [dlq.rows([v])[0] for v in dlq.versions()[1:]]
    expected = [(None, '"just a string"'), (None, '{"id":3}'), (None, "[]")]
    assert len(letters) == 3 and all(dead_letter_matches(r, e) for r, e in zip(letters, expected)), letters
    for row in letters:
        check_dead_letter_row(row, started, ended)

    assert_rows_by(plain.rows(), [{"id": 1, "iso": None}, {"id": 3, "iso": None}, {"id": 5, "iso": None}], key="id")
    assert plain.txns() == {"plain-0": 4}
    letters = [plain_dlq.rows([v])[0] for v in plain_dlq.versions()[1:]]
    expected = [(None, '"just a string"'), (None, "[]")]
    assert len(letters) == 2 and all(dead_letter_matches(r, e) for r, e in zip(letters, expected)), letters
