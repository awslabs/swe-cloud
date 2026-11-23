"""When buffered messages become commits: latency, batch-count and file-size triggers, offsets per commit."""

import time

from lake import assert_rows_by, check_lock_items, kinds, quiet, txn_actions

ID_ONLY = [("id", "integer")]


def ids(table, version):
    return sorted(r["id"] for r in table.rows([version]))


def test_latency_flush_commits_each_wave_and_repeats_known_offsets(lake):
    """Adapted from tests/buffer_flush_tests.rs (latency expiry, no empty versions)."""
    topic = lake.topic(partitions=2)
    table = lake.table(ID_ONLY)
    lake.produce(topic, [(0, {"id": 1}), (0, {"id": 2}), (1, {"id": 3})])
    started = time.time()
    lake.start(lake.args(topic, table, "-K", "client.id=wave-ingest", "--auto_offset_reset", "earliest", app="wave", latency=3))
    table.wait_version(1)
    quiet(table, 1, 5)  # an idle latency period writes no empty version

    first = table.commit(1)
    assert set(kinds(first)) - {"commitInfo"} == {"txn", "add"}
    assert txn_actions(first) == {("wave-0", 1): 1, ("wave-1", 0): 1}
    assert len(table.adds(1)) == 1
    table.check_adds(1)
    assert ids(table, 1) == [1, 2, 3]

    lake.produce(topic, [(1, {"id": 4})])
    table.wait_version(2)
    quiet(table, 2, 5)
    ended = time.time()
    second = table.commit(2)
    assert set(kinds(second)) - {"commitInfo"} == {"txn", "add"}
    # The partition without new messages keeps its recorded offset in the new commit.
    assert txn_actions(second) == {("wave-0", 1): 1, ("wave-1", 1): 1}
    assert ids(table, 2) == [4]
    table.check_adds(2)
    check_lock_items(lake, table, [1, 2], started, ended)


def test_full_batches_commit_without_waiting_for_latency(lake):
    """Adapted from test_flush_on_size_without_latency_expiration: -m completes batches, a tiny -b commits them."""
    topic = lake.topic()
    table = lake.table(ID_ONLY)
    lake.produce(topic, [(0, {"id": i}) for i in range(1, 6)])
    run = lake.start(lake.args(topic, table, "-m", 2, "-b", 1, app="count", latency=12))
    table.wait_version(2, timeout=30)
    first_two = time.time()
    assert table.versions() == [0, 1, 2], run.output()
    assert ids(table, 1) == [1, 2] and ids(table, 2) == [3, 4]
    assert table.txns(upto=1) == {"count-0": 1} and table.txns(upto=2) == {"count-0": 3}
    for v in (1, 2):
        assert txn_actions(table.commit(v)) == {("count-0", 2 * v - 1): 1}
    time.sleep(max(0.0, first_two + 6 - time.time()))
    assert table.versions() == [0, 1, 2], "the odd message must wait for the latency period"
    table.wait_version(3, timeout=20)
    assert ids(table, 3) == [5]
    assert table.txns() == {"count-0": 4}
    quiet(table, 3, 2)


def test_batch_count_alone_does_not_commit(lake):
    topic = lake.topic()
    table = lake.table(ID_ONLY)
    lake.produce(topic, [(0, {"id": i}) for i in range(1, 6)])
    lake.start(lake.args(topic, table, "-m", 2, "--json", "", app="batch", latency=8))
    time.sleep(5)
    assert table.versions() == [0], "a completed batch below the file size waits for the latency period"
    table.wait_version(1, timeout=25)
    quiet(table, 1, 3)
    assert ids(table, 1) == [1, 2, 3, 4, 5]
    assert len(table.adds(1)) == 1
    assert table.txns() == {"batch-0": 4}


def test_partitioned_rows_one_file_per_value_per_commit(lake):
    topic = lake.topic(partitions=2)
    table = lake.table([("id", "integer"), ("color", "string")], ["color"])
    lake.produce(topic, [(0, {"id": 1, "color": "red"}), (1, {"id": 2, "color": "red"}), (0, {"id": 3, "color": "blue"}),
                         (1, {"id": 4, "color": "blue"}), (1, {"id": 5, "color": "red"})])
    lake.start(lake.args(topic, table, app="files"))
    table.wait_version(1)
    quiet(table, 1, 4)
    table.check_adds(1)
    assert sorted(a["partitionValues"]["color"] for a in table.adds(1)) == ["blue", "red"]
    for add in table.adds(1):
        assert sorted(r["id"] for r in table.file_rows(add)) == {"red": [1, 2, 5], "blue": [3, 4]}[add["partitionValues"]["color"]]
    assert txn_actions(table.commit(1)) == {("files-0", 1): 1, ("files-1", 2): 1}
    assert_rows_by(table.rows(), [{"id": 1, "color": "red"}, {"id": 2, "color": "red"}, {"id": 3, "color": "blue"},
                                  {"id": 4, "color": "blue"}, {"id": 5, "color": "red"}], key="id")
