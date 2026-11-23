"""Where a run starts: offsets stored in the table, seek offsets, the reset policy, and resuming later runs."""

import time

from lake import assert_rows_by, kinds, quiet, txn_actions

FIELDS = [("id", "integer"), ("off", "long")]
OFFSET = ("-t", "off: kafka.offset")


def offsets(table, versions=None):
    return sorted(r["off"] for r in table.rows(versions))


def run_until(lake, table, args, version, settle=3):
    run = lake.start(args)
    table.wait_version(version)
    quiet(table, version, settle)
    run.stop()
    return run


def test_later_run_resumes_after_recorded_offset(lake):
    """Offsets come from the table's txn actions, not the consumer group: a new group resumes, nothing repeats."""
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": i}) for i in range(3)])
    run_until(lake, table, lake.args(topic, table, *OFFSET, app="resume"), 1)
    assert offsets(table) == [0, 1, 2] and table.txns() == {"resume-0": 2}

    lake.produce(topic, [(0, {"id": i}) for i in range(3, 6)])
    run_until(lake, table, lake.args(topic, table, *OFFSET, app="resume"), 2)
    assert offsets(table, [2]) == [3, 4, 5]
    assert txn_actions(table.commit(2)) == {("resume-0", 5): 1}
    assert_rows_by(table.rows(), [{"id": i, "off": i} for i in range(6)], key="id")


def test_new_app_id_ingests_from_the_reset_position(lake):
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": i}) for i in range(3)])
    run_until(lake, table, lake.args(topic, table, *OFFSET, app="first"), 1)
    run_until(lake, table, lake.args(topic, table, *OFFSET, app="second"), 2)
    assert offsets(table, [2]) == [0, 1, 2]
    assert table.txns() == {"first-0": 2, "second-0": 2}
    assert txn_actions(table.commit(2)) == {("second-0", 2): 1}


def test_seek_offsets_recorded_before_consuming(lake):
    """Adapted from tests/offset_tests.rs test_start_from_explicit: the first row ingested is seek offset + 1."""
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": i}) for i in range(8)])
    run_until(lake, table, lake.args(topic, table, *OFFSET, "--seek_offsets", '{"0":3}', app="seek"), 2)
    seek_commit = table.commit(1)
    assert set(kinds(seek_commit)) - {"commitInfo"} == {"txn"}
    assert txn_actions(seek_commit) == {("seek-0", 3): 1}
    assert offsets(table, [2]) == [4, 5, 6, 7]
    assert table.txns() == {"seek-0": 7}


def test_seek_offsets_behind_stored_progress_are_ignored(lake):
    topic = lake.topic()
    table = lake.table(FIELDS)
    table.put_log(1, '{"txn":{"appId":"behind-0","version":4,"lastUpdated":1700000000000}}\n')
    lake.produce(topic, [(0, {"id": i}) for i in range(7)])
    run_until(lake, table, lake.args(topic, table, *OFFSET, "--seek_offsets", '{"0":1}', app="behind"), 2)
    assert table.versions() == [0, 1, 2]
    assert offsets(table) == [5, 6]
    assert txn_actions(table.commit(2)) == {("behind-0", 6): 1}


def test_seek_offsets_ahead_of_stored_progress_fail(lake):
    topic = lake.topic()
    table = lake.table(FIELDS)
    table.put_log(1, '{"txn":{"appId":"ahead-0","version":4,"lastUpdated":1700000000000}}\n')
    lake.produce(topic, [(0, {"id": i}) for i in range(7)])
    run = lake.start(lake.args(topic, table, *OFFSET, "--seek_offsets", '{"0":5}', app="ahead"))
    assert run.wait(30) == 101, run.output()
    time.sleep(1)
    assert table.versions() == [0, 1]
    assert lake.lock_items(table) == []


def test_seek_offsets_skipped_when_any_partition_has_progress(lake):
    """With progress stored for some listed partitions, nothing is recorded: the others start from the reset position."""
    topic = lake.topic(partitions=2)
    table = lake.table([*FIELDS, ("p", "integer")])
    table.put_log(1, '{"txn":{"appId":"mixed-0","version":1,"lastUpdated":1700000000000}}\n')
    lake.produce(topic, [(0, {"id": 0}), (0, {"id": 1}), (0, {"id": 2}), (1, {"id": 10}), (1, {"id": 11}), (1, {"id": 12})])
    run_until(lake, table, lake.args(topic, table, *OFFSET, "-t", "p: kafka.partition", "--seek_offsets", '{"0":0,"1":1}', app="mixed"), 2)
    assert table.versions() == [0, 1, 2]
    assert sorted((r["p"], r["off"]) for r in table.rows()) == [(0, 2), (1, 0), (1, 1), (1, 2)]
    assert txn_actions(table.commit(2)) == {("mixed-0", 2): 1, ("mixed-1", 2): 1}


def test_stored_offset_zero_resumes_at_one(lake):
    """Adapted from tests/offset_tests.rs zero_offset_issue and tests/data/zero_offset."""
    topic = lake.topic()
    table = lake.table(FIELDS)
    table.put_log(1, '{"txn":{"appId":"zero-0","version":0,"lastUpdated":1700000000000}}\n')
    lake.produce(topic, [(0, {"id": i}) for i in range(3)])
    run_until(lake, table, lake.args(topic, table, *OFFSET, app="zero"), 2)
    assert offsets(table) == [1, 2]
    assert table.txns() == {"zero-0": 2}


def test_latest_reset_skips_existing_messages(lake):
    """Adapted from tests/offset_tests.rs test_start_from_latest."""
    topic = lake.topic()
    table = lake.table(FIELDS)
    lake.produce(topic, [(0, {"id": i}) for i in range(3)])
    run = lake.start(lake.args(topic, table, *OFFSET, "--auto_offset_reset", "latest", app="latest"))
    time.sleep(8)
    assert table.versions() == [0]
    lake.produce(topic, [(0, {"id": 3}), (0, {"id": 4})])
    table.wait_version(1)
    quiet(table, 1, 3)
    run.stop()
    assert offsets(table) == [3, 4]
    assert table.txns() == {"latest-0": 4}
