"""Balanced group consumer: subscription, exit at end of all assigned partitions, committed offsets."""

import json
import subprocess

import pytest
from support import by_partition, lines, same_json


@pytest.fixture
def two_topics(kafka):
    """t1 has partitions [a b c] and [d]; t2 has [x y]."""
    t1, t2 = kafka.topic(2), kafka.topic(1)
    assert kafka.run("-P", "-t", t1, "-p", "0", stdin=b"a\nb\nc\n").returncode == 0
    assert kafka.run("-P", "-t", t1, "-p", "1", stdin=b"d\n").returncode == 0
    assert kafka.run("-P", "-t", t2, "-p", "0", stdin=b"x\ny\n").returncode == 0
    return t1, t2


def group_rows(out):
    return by_partition(lines(out), lambda r: tuple(r.split(b":")[:2]))


def run_group(kafka, group, *args, timeout=45):
    result = kafka.run("-G", group, *args, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_group_reads_all_subscribed_topics_and_commits(kafka, two_topics):
    t1, t2 = two_topics
    group = kafka.group()
    out = run_group(kafka, group, "-o", "beginning", "-e", "-f", "%t:%p:%o:%s\\n", t1, t2)
    b1, b2 = t1.encode(), t2.encode()
    assert group_rows(out) == {
        (b1, b"0"): [b1 + b":0:0:a", b1 + b":0:1:b", b1 + b":0:2:c"],
        (b1, b"1"): [b1 + b":1:0:d"],
        (b2, b"0"): [b2 + b":0:0:x", b2 + b":0:1:y"],
    }
    assert kafka.committed(group, [(t1, 2), (t2, 1)]) == {(t1, 0): 3, (t1, 1): 1, (t2, 0): 2}


def test_group_resumes_from_committed_offsets(kafka, two_topics):
    t1, t2 = two_topics
    group = kafka.group()
    run_group(kafka, group, "-o", "beginning", "-e", t1, t2)
    assert kafka.run("-P", "-t", t1, "-p", "1", stdin=b"e\n").returncode == 0
    # Without -o the group continues from its committed positions.
    assert run_group(kafka, group, "-e", "-f", "%t:%p:%o:%s\\n", t1, t2) == t1.encode() + b":1:1:e\n"
    assert kafka.committed(group, [(t1, 2), (t2, 1)]) == {(t1, 0): 3, (t1, 1): 2, (t2, 0): 2}
    # "stored" is the same as the committed position: nothing is left.
    assert run_group(kafka, group, "-o", "stored", "-e", "-f", "%t:%p:%o:%s\\n", t1, t2) == b""
    # An explicit start offset overrides the committed one.
    out = run_group(kafka, group, "-o", "beginning", "-e", "-J", t2)
    rows = [json.loads(line) for line in lines(out)]
    leader = kafka.metadata().topics[t2].partitions[0].leader
    stored = kafka.records(t2)[0]
    assert len(rows) == 2
    for row, record in zip(rows, stored):
        same_json(row, {"topic": t2, "partition": 0, "offset": record["offset"], "tstype": "create", "ts": record["ts"],
                        "broker": leader, "key": None, "payload": record["value"].decode()})
    assert kafka.committed(group, [(t1, 2), (t2, 1)]) == {(t1, 0): 3, (t1, 1): 2, (t2, 0): 2}


def test_new_group_without_offset_starts_at_end(kafka, two_topics):
    t1, _ = two_topics
    group = kafka.group()
    assert run_group(kafka, group, "-e", "-f", "%t:%p:%o:%s\\n", t1) == b""
    assert kafka.committed(group, [(t1, 2)]) == {(t1, 0): None, (t1, 1): None}
    group = kafka.group()
    assert run_group(kafka, group, "-o", "end", "-e", "-f", "%t:%p:%o:%s\\n", t1) == b""
    assert kafka.committed(group, [(t1, 2)]) == {(t1, 0): None, (t1, 1): None}


def test_group_absolute_and_relative_offsets(kafka, two_topics):
    t1, _ = two_topics
    b1 = t1.encode()
    out = run_group(kafka, kafka.group(), "-o", "1", "-e", "-f", "%t:%p:%o:%s\\n", t1)
    assert group_rows(out) == {(b1, b"0"): [b1 + b":0:1:b", b1 + b":0:2:c"]}
    out = run_group(kafka, kafka.group(), "-o", "-1", "-e", "-f", "%t:%p:%o:%s\\n", t1)
    assert group_rows(out) == {(b1, b"0"): [b1 + b":0:2:c"], (b1, b"1"): [b1 + b":1:0:d"]}


def test_group_message_count_commits_consumed_records(kafka, two_topics):
    _, t2 = two_topics
    group = kafka.group()
    assert run_group(kafka, group, "-o", "beginning", "-c", "1", "-f", "%o:%s\\n", t2) == b"0:x\n"
    assert kafka.committed(group, [(t2, 1)]) == {(t2, 0): 1}


def test_group_exit_with_empty_partition(kafka):
    topic = kafka.topic(3)
    assert kafka.run("-P", "-t", topic, "-p", "2", stdin=b"only\n").returncode == 0
    group = kafka.group()
    assert run_group(kafka, group, "-o", "beginning", "-e", "-f", "%p:%o:%s\\n", topic) == b"2:0:only\n"
    assert kafka.committed(group, [(topic, 3)]) == {(topic, 0): None, (topic, 1): None, (topic, 2): 1}


def test_group_without_exit_flag_keeps_running(kafka, two_topics):
    # Adapted from upstream tests/0001-exit_eof.sh.
    _, t2 = two_topics
    with pytest.raises(subprocess.TimeoutExpired) as stopped:
        kafka.run("-G", kafka.group(), "-o", "beginning", "-u", "-f", "%s\\n", t2, timeout=12)
    assert stopped.value.stdout == b"x\ny\n"


def test_group_refusals(kafka, two_topics):
    t1, _ = two_topics
    group = kafka.group()
    for args in (["-o", "s@1", "-e", t1], ["-o", "beginning", "-o", "e@99999999999999", "-e", t1], ["-o", "beginning", "-e"]):
        result = kafka.run("-G", group, *args, timeout=30)
        assert result.returncode == 1, args
        assert result.stdout == b""
    assert kafka.committed(group, [(t1, 2)]) == {(t1, 0): None, (t1, 1): None}
