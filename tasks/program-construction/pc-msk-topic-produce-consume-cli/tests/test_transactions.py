"""Transactional producer: one committed transaction per run."""

from support import lines


def assert_committed_transaction(kafka, topic, partition, expected, runs=None):
    """expected: [(key, value, headers)] visible under read_committed.

    runs: sizes of the consecutive groups written by separate runs (default: one run).
    Each run's records are contiguous, and exactly one control record (the commit
    marker) follows the last run's data.
    """
    committed = kafka.records(topic, "read_committed")[partition]
    uncommitted = kafka.records(topic, "read_uncommitted")[partition]
    assert [(r["key"], r["value"], r["headers"]) for r in committed] == expected
    assert [r["offset"] for r in uncommitted] == [r["offset"] for r in committed]
    offsets = [r["offset"] for r in committed]
    start = 0
    for size in runs or [len(offsets)]:
        run = offsets[start:start + size]
        assert run == list(range(run[0], run[0] + size)), offsets
        start += size
    assert start == len(offsets)
    _, high = kafka.watermarks(topic, partition)
    assert high == offsets[-1] + 2, (offsets, high)


def test_transaction_commits_all_records(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-X", f"transactional.id={kafka.name('tx')}", stdin=b"a\nb\nc\n")
    assert result.returncode == 0, result.stderr
    assert_committed_transaction(kafka, topic, 0, [(None, b"a", []), (None, b"b", []), (None, b"c", [])])
    out = kafka.run("-C", "-t", topic, "-o", "beginning", "-e", "-f", "%s\\n")
    assert out.returncode == 0 and lines(out.stdout) == [b"a", b"b", b"c"]


def test_transaction_with_keys_headers_partition_and_count(kafka):
    topic = kafka.topic(2)
    args = ["-P", "-t", topic, "-p", "1", "-K", ":", "-H", "h=v", "-c", "2", "-X", f"transactional.id={kafka.name('tx')}"]
    result = kafka.run(*args, stdin=b"k1:one\ntwo\nk3:three\n")
    assert result.returncode == 0, result.stderr
    assert kafka.records(topic, "read_uncommitted")[0] == []
    assert_committed_transaction(kafka, topic, 1, [(b"k1", b"one", [("h", b"v")]), (None, b"two", [("h", b"v")])])


def test_repeated_runs_with_same_transactional_id(kafka):
    topic = kafka.topic()
    tid = kafka.name("tx")
    for batch in (b"r1\n", b"r2a\nr2b\n"):
        result = kafka.run("-P", "-t", topic, "-X", f"transactional.id={tid}", stdin=batch)
        assert result.returncode == 0, result.stderr
    assert_committed_transaction(kafka, topic, 0, [(None, b"r1", []), (None, b"r2a", []), (None, b"r2b", [])], runs=[1, 2])
    # A later non-transactional record directly follows the last commit marker.
    _, high = kafka.watermarks(topic, 0)
    assert kafka.run("-P", "-t", topic, stdin=b"plain\n").returncode == 0
    assert [(r["offset"], r["value"]) for r in kafka.records(topic, "read_committed")[0]][-1] == (high, b"plain")


def test_empty_input_commits_nothing(kafka):
    topic = kafka.topic()
    result = kafka.run("-P", "-t", topic, "-X", f"transactional.id={kafka.name('tx')}", stdin=b"")
    assert result.returncode == 0, result.stderr
    assert kafka.records(topic)[0] == []
