"""Simple consumer: output rendering (-f, plain, -Z, -J) and record selection (-o, -c, -e, -p)."""

import json
import subprocess
import time

import pytest
from harness import start_command
from support import by_partition, lines, same_json


@pytest.fixture
def keyed(kafka):
    """Partition 0: four records with explicit timestamps (one NULL key); partition 1: one; partition 2: empty."""
    topic = kafka.topic(3)
    kafka.produce(topic, [
        {"partition": 0, "key": b"ka", "value": b"a0", "timestamp": 1000},
        {"partition": 0, "key": b"kb", "value": b"a1", "timestamp": 1001},
        {"partition": 0, "key": None, "value": b"a2", "timestamp": 1002},
        {"partition": 0, "key": b"kd", "value": b"a3", "timestamp": 1003},
        {"partition": 1, "key": b"x", "value": b"b0", "timestamp": 2000},
    ])
    return topic


@pytest.fixture
def nulls(kafka):
    """One partition holding empty and NULL keys/values plus headers."""
    topic = kafka.topic()
    kafka.produce(topic, [
        {"partition": 0, "key": b"The First", "value": b"Message1", "timestamp": 5000},
        {"partition": 0, "key": b"Is The", "value": b"", "timestamp": 5001},
        {"partition": 0, "key": b"", "value": b"Greatest", "timestamp": 5002},
        {"partition": 0, "key": None, "value": b"For sure", "timestamp": 5003},
        {"partition": 0, "key": b"k", "value": None, "timestamp": 5004,
         "headers": [("a", b"1"), ("bare", None), ("empty", b""), ("a", b"2"), ("eq", b"x=y")]},
    ])
    return topic


@pytest.fixture
def timed(kafka):
    """Two partitions with CreateTime timestamps on a 100000 ms grid."""
    topic = kafka.topic(2)
    kafka.produce(topic, [
        {"partition": 0, "key": b"k", "value": b"t%d" % t, "timestamp": t * 1000} for t in (100, 200, 300, 400)
    ] + [
        {"partition": 1, "key": b"k", "value": b"u%d" % t, "timestamp": t * 1000} for t in (150, 250, 350)
    ])
    return topic


def consume(kafka, topic, *args, **kwargs):
    result = kafka.run("-C", "-t", topic, *args, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_format_tokens_and_escapes(kafka, keyed):
    out = consume(kafka, keyed, "-p", "0", "-o", "beginning", "-e", "-f", "%t|%p|%o|%T|%k|%K|%s|%S|%%|\\t|\\x41\\x4a|%h\\n")
    t = keyed.encode()
    assert out == (
        t + b"|0|0|1000|ka|2|a0|2|%|\t|AJ|\n"
        + t + b"|0|1|1001|kb|2|a1|2|%|\t|AJ|\n"
        + t + b"|0|2|1002||-1|a2|2|%|\t|AJ|\n"
        + t + b"|0|3|1003|kd|2|a3|2|%|\t|AJ|\n"
    )


def test_format_without_trailing_newline_and_binary_length(kafka, keyed):
    assert consume(kafka, keyed, "-p", "0", "-o", "beginning", "-e", "-f", "%s") == b"a0a1a2a3"
    assert consume(kafka, keyed, "-p", "1", "-o", "beginning", "-e", "-f", "%R%s\\r\\n") == b"\x00\x00\x00\x02b0\r\n"


def test_format_lengths_headers_and_null_rendering(kafka, nulls):
    fmt = "[%k|%K|%s|%S|%h]\\n"
    assert consume(kafka, nulls, "-o", "beginning", "-e", "-f", fmt) == (
        b"[The First|9|Message1|8|]\n"
        b"[Is The|6||0|]\n"
        b"[|0|Greatest|8|]\n"
        b"[|-1|For sure|8|]\n"
        b"[k|1||-1|a=1,bare=NULL,empty=,a=2,eq=x=y]\n"
    )
    # With -Z both empty and NULL keys/values print as NULL; lengths still tell them apart.
    assert consume(kafka, nulls, "-o", "beginning", "-e", "-Z", "-f", fmt) == (
        b"[The First|9|Message1|8|]\n"
        b"[Is The|6|NULL|0|]\n"
        b"[NULL|0|Greatest|8|]\n"
        b"[NULL|-1|For sure|8|]\n"
        b"[k|1|NULL|-1|a=1,bare=NULL,empty=,a=2,eq=x=y]\n"
    )
    assert consume(kafka, nulls, "-o", "-1", "-e", "-f", "%R|%s|%S") == b"\xff\xff\xff\xff||-1"


def test_plain_output_delimiters(kafka, nulls):
    assert consume(kafka, nulls, "-o", "beginning", "-e") == b"Message1\n\nGreatest\nFor sure\n\n"
    assert consume(kafka, nulls, "-o", "beginning", "-e", "-K", "=>", "-D", "|") == b"The First=>Message1|Is The=>|=>Greatest|=>For sure|k=>|"
    assert consume(kafka, nulls, "-o", "beginning", "-e", "-K", "=>", "-Z") == (
        b"The First=>Message1\nIs The=>NULL\nNULL=>Greatest\nNULL=>For sure\nk=>NULL\n"
    )
    assert consume(kafka, nulls, "-o", "-2", "-e", "-D", "\\x2c\\t", "-K", "\\x3d") == b"=For sure,\tk=,\t"
    # A format string overrides -K/-D.
    assert consume(kafka, nulls, "-o", "-1", "-e", "-K", ":", "-D", "|", "-f", "<%k>\\n") == b"<k>\n"


def test_bad_format_strings(kafka, keyed):
    for fmt in ("%Q", "abc%"):
        result = kafka.run("-C", "-t", keyed, "-p", "1", "-o", "beginning", "-e", "-f", fmt)
        assert result.returncode == 1, fmt
        assert result.stdout == b""


def envelope(topic, partition, record, leader, **extra):
    return {"topic": topic, "partition": partition, "offset": record["offset"], "tstype": "create", "ts": record["ts"],
            "broker": leader, "key": record["key"], "payload": record["value"], **extra}


def leader_of(kafka, topic, partition):
    return kafka.metadata().topics[topic].partitions[partition].leader


def test_json_envelope_types_nulls_and_headers(kafka, nulls):
    out = consume(kafka, nulls, "-o", "beginning", "-e", "-J")
    leader = leader_of(kafka, nulls, 0)
    expected = [
        (0, "The First", "Message1", 5000, None),
        (1, "Is The", "", 5001, None),
        (2, "", "Greatest", 5002, None),
        (3, None, "For sure", 5003, None),
        (4, "k", None, 5004, ["a", "1", "bare", None, "empty", "", "a", "2", "eq", "x=y"]),
    ]
    rows = [json.loads(line) for line in lines(out)]
    assert len(rows) == len(expected)
    for row, (offset, key, payload, ts, headers) in zip(rows, expected):
        want = {"topic": nulls, "partition": 0, "offset": offset, "tstype": "create", "ts": ts, "broker": leader, "key": key, "payload": payload}
        if headers is not None:
            want["headers"] = headers
        same_json(row, want)


def test_json_envelope_escaping_and_custom_separator(kafka):
    topic = kafka.topic()
    kafka.produce(topic, [
        {"partition": 0, "key": b'q"k', "value": b'q"uo\\te\tTAB/\x01ctl\xc3\xa9', "timestamp": 7000},
        {"partition": 0, "key": None, "value": b"k\x00z\n", "timestamp": 7001},
    ])
    out = consume(kafka, topic, "-o", "beginning", "-e", "-J", "-D", "~~")
    leader = leader_of(kafka, topic, 0)
    assert out.endswith(b"~~") and out.count(b"~~") == 2
    rows = [json.loads(chunk) for chunk in out.split(b"~~")[:-1]]
    same_json(rows[0], {"topic": topic, "partition": 0, "offset": 0, "tstype": "create", "ts": 7000, "broker": leader, "key": 'q"k', "payload": 'q"uo\\te\tTAB/\x01ctlé'})
    same_json(rows[1], {"topic": topic, "partition": 0, "offset": 1, "tstype": "create", "ts": 7001, "broker": leader, "key": None, "payload": "k\x00z\n"})


def test_json_envelope_log_append_time(kafka):
    topic = kafka.topic(config={"message.timestamp.type": "LogAppendTime"})
    assert kafka.run("-P", "-t", topic, stdin=b"l1\n").returncode == 0
    stored = kafka.records(topic)[0]
    rows = [json.loads(line) for line in lines(consume(kafka, topic, "-o", "beginning", "-e", "-J"))]
    assert len(rows) == 1 and len(stored) == 1
    same_json(rows[0], {"topic": topic, "partition": 0, "offset": 0, "tstype": "logappend", "ts": stored[0]["ts"],
                        "broker": leader_of(kafka, topic, 0), "key": None, "payload": "l1"})


def test_offsets_beginning_end_absolute_and_default(kafka, keyed):
    fmt = ["-f", "%p:%o:%s\\n"]
    assert consume(kafka, keyed, "-o", "end", "-e", *fmt) == b""
    assert consume(kafka, keyed, "-p", "0", "-o", "2", "-e", *fmt) == b"0:2:a2\n0:3:a3\n"
    assert consume(kafka, keyed, "-p", "0", "-o", "0", "-e", *fmt) == b"0:0:a0\n0:1:a1\n0:2:a2\n0:3:a3\n"
    # Without -o every partition is read from its beginning.
    rows = by_partition(lines(consume(kafka, keyed, "-e", *fmt)), lambda r: r.split(b":")[0])
    assert rows == {b"0": [b"0:0:a0", b"0:1:a1", b"0:2:a2", b"0:3:a3"], b"1": [b"1:0:b0"]}
    # An absolute offset applies to every partition; past the end there is nothing to read.
    rows = by_partition(lines(consume(kafka, keyed, "-o", "1", "-e", *fmt)), lambda r: r.split(b":")[0])
    assert rows == {b"0": [b"0:1:a1", b"0:2:a2", b"0:3:a3"]}
    assert consume(kafka, keyed, "-p", "0", "-o", "10", "-e", *fmt) == b""


def test_relative_offsets(kafka, keyed):
    fmt = ["-f", "%p:%o:%s\\n"]
    rows = by_partition(lines(consume(kafka, keyed, "-o", "-2", "-e", *fmt)), lambda r: r.split(b":")[0])
    assert rows == {b"0": [b"0:2:a2", b"0:3:a3"], b"1": [b"1:0:b0"]}
    assert consume(kafka, keyed, "-p", "0", "-o", "-10", "-e", *fmt) == b"0:0:a0\n0:1:a1\n0:2:a2\n0:3:a3\n"
    assert consume(kafka, keyed, "-p", "0", "-o", "-1", "-e", *fmt) == b"0:3:a3\n"


def test_message_count(kafka, keyed):
    fmt = ["-f", "%o:%s\\n"]
    # -c alone stops after the count even though more records exist.
    assert consume(kafka, keyed, "-p", "0", "-o", "beginning", "-c", "2", *fmt) == b"0:a0\n1:a1\n"
    assert consume(kafka, keyed, "-p", "0", "-o", "1", "-c", "1", *fmt) == b"1:a1\n"
    # -e ends the run first when fewer records remain.
    assert consume(kafka, keyed, "-p", "0", "-o", "3", "-c", "5", "-e", *fmt) == b"3:a3\n"


def test_exit_at_end_of_empty_partitions(kafka):
    topic = kafka.topic(2)
    assert consume(kafka, topic, "-o", "beginning", "-e") == b""


def test_without_exit_flag_consumer_keeps_running(kafka, keyed):
    # Adapted from upstream tests/0001-exit_eof.sh; -u makes already-written records observable.
    with pytest.raises(subprocess.TimeoutExpired) as stopped:
        kafka.run("-C", "-t", keyed, "-p", "0", "-o", "beginning", "-u", "-f", "%s\\n", timeout=8)
    assert stopped.value.stdout == b"a0\na1\na2\na3\n"


def test_waits_for_new_records_from_end(kafka):
    topic = kafka.topic()
    kafka.produce(topic, [{"partition": 0, "value": b"old"}])
    process = start_command(["/app/program", "-b", kafka.bootstrap, "-C", "-t", topic, "-o", "end", "-c", "1", "-f", "%o:%s\\n"],
                            env={"HOME": kafka.home}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        time.sleep(5)
        kafka.produce(topic, [{"partition": 0, "value": b"late"}])
        out, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 0
    assert out == b"1:late\n"


def test_missing_partition_and_topic(kafka, keyed):
    result = kafka.run("-C", "-t", keyed, "-p", "5", "-o", "beginning", "-e")
    assert result.returncode == 1 and result.stdout == b""
    result = kafka.run("-C", "-t", kafka.name("absent"), "-o", "beginning", "-e")
    assert result.returncode == 1 and result.stdout == b""


def test_start_timestamp(kafka, timed):
    fmt = ["-f", "%p:%o:%T:%s\\n"]
    rows = by_partition(lines(consume(kafka, timed, "-o", "s@200000", "-e", *fmt)), lambda r: r.split(b":")[0])
    assert rows == {b"0": [b"0:1:200000:t200", b"0:2:300000:t300", b"0:3:400000:t400"], b"1": [b"1:1:250000:u250", b"1:2:350000:u350"]}
    assert consume(kafka, timed, "-p", "1", "-o", "s@200000", "-e", *fmt) == b"1:1:250000:u250\n1:2:350000:u350\n"
    assert consume(kafka, timed, "-p", "0", "-o", "s@300000", "-e", *fmt) == b"0:2:300000:t300\n0:3:400000:t400\n"
    assert consume(kafka, timed, "-o", "s@900000", "-e", *fmt) == b""


def test_stop_timestamp_ends_run_without_exit_flag(kafka, timed):
    fmt = ["-f", "%p:%o:%T:%s\\n"]
    rows = by_partition(lines(consume(kafka, timed, "-o", "beginning", "-o", "e@300000", *fmt)), lambda r: r.split(b":")[0])
    assert rows == {b"0": [b"0:0:100000:t100", b"0:1:200000:t200"], b"1": [b"1:0:150000:u150", b"1:1:250000:u250"]}
    assert consume(kafka, timed, "-o", "beginning", "-o", "e@50", *fmt) == b""


def test_timestamp_window(kafka, timed):
    fmt = ["-f", "%p:%o:%T:%s\\n"]
    rows = by_partition(lines(consume(kafka, timed, "-o", "s@150000", "-o", "e@350000", "-e", *fmt)), lambda r: r.split(b":")[0])
    assert rows == {b"0": [b"0:1:200000:t200", b"0:2:300000:t300"], b"1": [b"1:0:150000:u150", b"1:1:250000:u250"]}
    out = consume(kafka, timed, "-p", "1", "-o", "s@150000", "-o", "e@350000", "-e", "-J")
    leader = leader_of(kafka, timed, 1)
    rows = [json.loads(line) for line in lines(out)]
    assert len(rows) == 2
    for row, (offset, ts) in zip(rows, [(0, 150000), (1, 250000)]):
        same_json(row, {"topic": timed, "partition": 1, "offset": offset, "tstype": "create", "ts": ts, "broker": leader, "key": "k", "payload": "u%d" % (ts // 1000)})


def test_stored_offsets_need_a_group(kafka, keyed):
    result = kafka.run("-C", "-t", keyed, "-p", "0", "-o", "stored", "-e")
    assert result.returncode == 1 and result.stdout == b""
    group = kafka.group()
    kafka.commit(group, keyed, {0: 2})
    out = consume(kafka, keyed, "-p", "0", "-o", "stored", "-X", f"group.id={group}", "-e", "-f", "%o:%s\\n")
    assert out == b"2:a2\n3:a3\n"
    # The consumed position is committed for the group.
    assert kafka.committed(group, [(keyed, 3)]) == {(keyed, 0): 4, (keyed, 1): None, (keyed, 2): None}
