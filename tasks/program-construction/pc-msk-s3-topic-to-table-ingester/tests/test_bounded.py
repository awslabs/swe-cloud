"""Bounded runs (--ends_at_latest_offsets): when they end, what they commit, and the exit status."""

import time

import pytest
from lake import quiet, txn_actions

FIELDS = [("id", "integer"), ("off", "long")]
OFFSET = ("-t", "off: kafka.offset")


def offsets(table, versions=None):
    return sorted(r["off"] for r in table.rows(versions))


@pytest.mark.timeout(600)  # the first test to use the session MSK cluster also waits for it to become ACTIVE
def test_bounded_run_ends_at_startup_end_offset(lake):
    """Adapted from tests/offset_tests.rs end_at_initial_offsets: the message at the start-up end offset ends the run."""
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": i}) for i in range(3)])
    run = lake.start(lake.args(topic, table, *OFFSET, "--ends_at_latest_offsets", app="bounded", latency=4))
    table.wait_version(1)
    time.sleep(1)
    assert run.poll() is None, "the run must wait for the end offset"
    lake.produce(topic, [(0, {"id": 3})])
    assert run.wait(20) == 0, run.output()
    assert table.versions() == [0, 1]
    assert offsets(table) == [0, 1, 2]
    assert table.txns() == {"bounded-0": 2}

    # The end message was consumed but not committed; a later run picks it up.
    follow = lake.start(lake.args(topic, table, *OFFSET, app="bounded"))
    table.wait_version(2)
    quiet(table, 2, 3)
    follow.stop()
    assert offsets(table, [2]) == [3]
    assert table.txns() == {"bounded-0": 3}


def test_bounded_run_reaching_end_before_a_flush_commits_nothing(lake):
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": 0}), (0, {"id": 1})])
    run = lake.start(lake.args(topic, table, *OFFSET, "-e", app="early", latency=25))
    time.sleep(7)
    assert run.poll() is None
    lake.produce(topic, [(0, {"id": 2})])
    assert run.wait(15) == 0, run.output()
    assert table.versions() == [0]
    assert lake.lock_items(table) == []


def test_bounded_run_flushes_the_end_message_when_its_batch_completes(lake):
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": 0}), (0, {"id": 1})])
    run = lake.start(lake.args(topic, table, *OFFSET, "-e", "-m", 1, "-b", 1, app="eager", latency=30))
    table.wait_version(2)
    time.sleep(1)
    assert run.poll() is None
    lake.produce(topic, [(0, {"id": 2})])
    assert run.wait(20) == 0, run.output()
    assert table.versions() == [0, 1, 2, 3]
    assert [offsets(table, [v]) for v in (1, 2, 3)] == [[0], [1], [2]]
    assert [txn_actions(table.commit(v)) for v in (1, 2, 3)] == [{("eager-0", v): 1} for v in (0, 1, 2)]


def test_bounded_run_waits_for_every_partition(lake):
    """A partition that is empty at start-up ends only when a message reaches its end offset (0)."""
    topic = lake.topic(partitions=2)
    table = lake.table([*FIELDS, ("p", "integer")])
    lake.produce(topic, [(0, {"id": 0}), (0, {"id": 1})])
    run = lake.start(lake.args(topic, table, *OFFSET, "-t", "p: kafka.partition", "-e", app="every", latency=4))
    table.wait_version(1)
    time.sleep(1)
    lake.produce(topic, [(0, {"id": 2})])
    # Partition 0's end message stays buffered in the still-running process and is flushed by latency.
    table.wait_version(2, timeout=15)
    assert run.poll() is None, "partition 1 has not reached its end offset"
    assert sorted((r["p"], r["off"]) for r in table.rows([2])) == [(0, 2)]
    lake.produce(topic, [(1, {"id": 3})])
    assert run.wait(20) == 0, run.output()
    assert table.versions() == [0, 1, 2]
    assert table.txns() == {"every-0": 2}


def test_bounded_run_with_dead_letters_on_two_partitions(lake):
    topic = lake.topic(partitions=2)
    table = lake.table([("id", "integer")])
    dlq = lake.table([("base64_bytes", "string"), ("json_string", "string"), ("error", "string"), ("timestamp", "timestamp")])
    lake.produce(topic, [(0, {"id": 0}), (0, b"bad-0"), (0, {"id": 2}), (1, {"id": 10}), (1, b"bad-1"), (1, {"id": 12})])
    run = lake.start(lake.args(topic, table, "--dlq_table_location", dlq.uri, "-e", app="mixed", latency=4))
    table.wait_version(1)
    lake.produce(topic, [(0, {"id": 3}), (1, {"id": 13})])
    assert run.wait(20) == 0, run.output()
    assert table.versions() == [0, 1]
    assert sorted(r["id"] for r in table.rows()) == [0, 2, 10, 12]
    assert txn_actions(table.commit(1)) == {("mixed-0", 2): 1, ("mixed-1", 2): 1}
    assert sorted(r["base64_bytes"] for r in dlq.rows()) == ["YmFkLTA=", "YmFkLTE="]
    assert all(r["json_string"] is None and isinstance(r["error"], str) and r["error"] for r in dlq.rows())
    assert dlq.versions() == [0, 1, 2]
