"""Group evaluation over commit windows: partition rules, group aggregation and the result cache.

Commit spacing is chosen so that each window is evaluated well inside (or well past) the
time span its commits cover; the service is always queried right after the last commit
becomes visible unless a test waits on purpose.
"""

import time

import pytest

from lagsvc import CLUSTER, by_partition, number, unique

STATUS_KEYS = {"cluster", "group", "status", "complete", "partitions", "partition_count", "maxlag", "totallag"}
PARTITION_KEYS = {"topic", "partition", "owner", "client_id", "status", "start", "end", "current_lag", "complete"}
GAP = 1.5  # seconds between commits of one window


def evaluation(svc, group, kind="lag"):
    body = svc.json(f"/v3/kafka/{CLUSTER}/consumer/{group}/{kind}")
    assert set(body) == {"error", "message", "status", "request"}
    assert body["error"] is False and body["message"] == "consumer status returned"
    assert body["request"]["url"] == f"/v3/kafka/{CLUSTER}/consumer/{group}/{kind}"
    status = body["status"]
    assert set(status) == STATUS_KEYS, status
    assert status["cluster"] == CLUSTER and status["group"] == group
    assert all(numeric(status[key]) for key in ("complete", "partition_count", "totallag")), status
    for item in status["partitions"] + ([status["maxlag"]] if status["maxlag"] else []):
        assert set(item) == PARTITION_KEYS, item
        assert all(numeric(item[key]) for key in ("partition", "current_lag", "complete")), item
        for edge in ("start", "end"):
            assert item[edge] is None or set(item[edge]) == {"offset", "timestamp", "observedAt", "lag"}
            if item[edge] is not None:
                assert all(numeric(item[edge][key]) for key in ("offset", "timestamp", "observedAt")), item
                assert item[edge]["lag"] is None or numeric(item[edge]["lag"]), item
    return status


def numeric(value):
    return type(value) in (int, float)


def check_partition(item, status, offsets, current_lag, complete, end_lag=None):
    """offsets = (start offset, end offset) or None for a partition without commits."""
    assert item["status"] == status, item
    assert type(item["current_lag"]) in (int, float) and item["current_lag"] == current_lag, item
    assert number(item["complete"], complete), item
    assert item["owner"] == "" and item["client_id"] == ""
    if offsets is None:
        assert item["start"] is None and item["end"] is None, item
    else:
        assert (item["start"]["offset"], item["end"]["offset"]) == offsets, item
        assert item["start"]["timestamp"] <= item["end"]["timestamp"]
        if end_lag is not None:
            assert item["end"]["lag"] == end_lag, item


def commits(kafka, svc, group, topic, sequence, partition=0):
    """Commit offsets one by one, GAP apart, waiting until each is stored."""
    stored = [entry["offset"] for entry in svc.window(group, topic, partition)]
    for index, offset in enumerate(sequence):
        if index:
            time.sleep(GAP)
        kafka.commit(group, topic, {partition: offset})
        stored.append(offset)
        svc.wait_window(group, topic, partition, stored)


def test_stalled_then_stopped(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 5})
    svc.wait_topic(topic, [10])
    group = unique("stall")
    commits(kafka, svc, group, topic, [3, 3, 3])
    status = evaluation(svc, group)
    assert status["status"] == "ERR" and status["partition_count"] == 1 and status["totallag"] == 7
    assert number(status["complete"], 0)
    (item,) = status["partitions"]
    assert (item["topic"], item["partition"]) == (topic, 0)
    check_partition(item, "STALL", (3, 3), 7, 0.6, end_lag=7)
    assert status["maxlag"] == item
    assert evaluation(svc, group, "status")["partitions"] == [item]

    # Once no commit arrived for longer than the window spans, the partition is stopped.
    span = (item["end"]["timestamp"] - item["start"]["timestamp"]) / 1000
    time.sleep(span + 2)
    status = evaluation(svc, group, "status")
    assert status["status"] == "ERR"
    (item,) = status["partitions"]
    check_partition(item, "STOP", (3, 3), 7, 0.6)


def test_single_lagging_commit_stops(kafka, service, topics):
    topic = topics(2)
    kafka.produce(topic, {0: 10, 1: 6})
    svc = service(storage={"intervals": 10})
    svc.wait_topic(topic, [10, 6])
    group = unique("single")
    kafka.commit(group, topic, {0: 4, 1: 6})
    svc.wait_window(group, topic, 0, [4])
    svc.wait_window(group, topic, 1, [6])
    time.sleep(2)
    status = evaluation(svc, group)
    assert status["status"] == "ERR" and status["totallag"] == 6 and status["partition_count"] == 2
    parts = by_partition(status)
    check_partition(parts[(topic, 0)], "STOP", (4, 4), 6, 0.1, end_lag=6)
    check_partition(parts[(topic, 1)], "OK", (6, 6), 0, 0.1, end_lag=0)
    assert status["maxlag"]["partition"] == 0 and status["maxlag"]["status"] == "STOP"
    filtered = evaluation(svc, group, "status")
    assert [(p["topic"], p["partition"]) for p in filtered["partitions"]] == [(topic, 0)]
    assert filtered["partition_count"] == 2 and filtered["totallag"] == 6 and filtered["maxlag"]["partition"] == 0


def test_rewind_and_recovery(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 4})
    svc.wait_topic(topic, [10])
    group = unique("rewind")
    commits(kafka, svc, group, topic, [8, 3])
    status = evaluation(svc, group)
    assert status["status"] == "ERR" and status["totallag"] == 7
    (item,) = status["partitions"]
    check_partition(item, "REWIND", (8, 3), 7, 0.5, end_lag=7)

    # A rewound partition whose commits then cease is reported as stopped.
    span = (item["end"]["timestamp"] - item["start"]["timestamp"]) / 1000
    time.sleep(span + 2)
    check_partition(evaluation(svc, group)["partitions"][0], "STOP", (8, 3), 7, 0.5)

    kafka.commit(group, topic, {0: 9})
    svc.wait_window(group, topic, 0, [8, 3, 9])
    time.sleep(1.1)  # outlive the cached STOP evaluation
    status = evaluation(svc, group)
    assert status["status"] == "OK" and status["totallag"] == 1
    (item,) = status["partitions"]
    check_partition(item, "OK", (8, 9), 1, 0.75, end_lag=1)
    assert evaluation(svc, group, "status")["partitions"] == []


def test_growing_lag_warns(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 5})
    svc.wait_topic(topic, [10])
    group = unique("warn")
    kafka.commit(group, topic, {0: 2})
    svc.wait_window(group, topic, 0, [2])
    kafka.produce(topic, {0: 10})
    svc.wait_topic(topic, [20])
    kafka.commit(group, topic, {0: 5})
    svc.wait_window(group, topic, 0, [2, 5])
    time.sleep(1)
    kafka.produce(topic, {0: 10})
    svc.wait_topic(topic, [30])
    kafka.commit(group, topic, {0: 8})
    window = svc.wait_window(group, topic, 0, [2, 5, 8])
    assert [entry["lag"] for entry in window] == [8, 15, 22]
    status = evaluation(svc, group)
    assert status["status"] == "WARN" and status["totallag"] == 22
    (item,) = status["partitions"]
    check_partition(item, "WARN", (2, 8), 22, 0.6, end_lag=22)
    assert evaluation(svc, group, "status")["partitions"] == [item]

    span = (window[-1]["timestamp"] - window[0]["timestamp"]) / 1000
    time.sleep(span + 2)
    status = evaluation(svc, group)
    assert status["status"] == "ERR"
    check_partition(status["partitions"][0], "STOP", (2, 8), 22, 0.6)


def test_shrinking_or_absent_lag_is_ok(kafka, service, topics):
    topic = topics(2)
    kafka.produce(topic, {0: 10, 1: 5})
    svc = service(storage={"intervals": 5})
    svc.wait_topic(topic, [10, 5])
    shrinking, caught_up = unique("shrink"), unique("caught")
    kafka.commit(caught_up, topic, {1: 5})
    commits(kafka, svc, shrinking, topic, [2, 6])
    status = evaluation(svc, shrinking)
    assert status["status"] == "OK" and status["totallag"] == 4 and status["partition_count"] == 2
    parts = by_partition(status)
    check_partition(parts[(topic, 0)], "OK", (2, 6), 4, 0.4, end_lag=4)
    check_partition(parts[(topic, 1)], "OK", None, 0, 0)
    assert status["maxlag"]["partition"] == 0
    filtered = evaluation(svc, shrinking, "status")
    assert filtered["partitions"] == [] and filtered["maxlag"]["partition"] == 0 and filtered["status"] == "OK"

    svc.wait_window(caught_up, topic, 1, [5])
    time.sleep(2)
    status = evaluation(svc, caught_up)
    assert status["status"] == "OK" and status["totallag"] == 0
    parts = by_partition(status)
    check_partition(parts[(topic, 1)], "OK", (5, 5), 0, 0.2, end_lag=0)
    check_partition(parts[(topic, 0)], "OK", None, 0, 0)


# STOP deadline / still-OK checkpoint (seconds after the new end offset is observed). The
# retained broker offsets advance once per 1 s poll, so STOP comes ~intervals-1..intervals s
# later: no fixed delay or fixed-size history fits both "stopped by 5.5 s" and "OK at 6.5 s".
RECENT_BOUNDS = {3: (5.5, None), 10: (16.0, 6.5)}


@pytest.mark.parametrize("intervals", sorted(RECENT_BOUNDS))
def test_recent_zero_lag_delays_stop(kafka, service, topics, intervals):
    """The delay before STOP follows the number of retained broker offsets (one per poll), not a fixed time."""
    stop_by, still_ok_at = RECENT_BOUNDS[intervals]
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": intervals})
    svc.wait_topic(topic, [10])
    group = unique("recent")
    kafka.commit(group, topic, {0: 10})
    svc.wait_window(group, topic, 0, [10])
    kafka.produce(topic, {0: 5})
    svc.wait_topic(topic, [15])
    moved = time.time()
    status = evaluation(svc, group)
    (item,) = status["partitions"]
    check_partition(item, "OK", (10, 10), 5, 1 / intervals, end_lag=0)
    assert status["status"] == "OK" and status["totallag"] == 5

    # Still OK while the retained broker offsets include the committed position.
    time.sleep(1.1)
    check_partition(evaluation(svc, group)["partitions"][0], "OK", (10, 10), 5, 1 / intervals)
    if still_ok_at is not None:
        time.sleep(max(0.0, moved + still_ok_at - time.time()))
        check_partition(evaluation(svc, group)["partitions"][0], "OK", (10, 10), 5, 1 / intervals)

    # Once they no longer do, it is stopped.
    while True:
        status = evaluation(svc, group)
        if status["partitions"][0]["status"] != "OK" or time.time() > moved + stop_by:
            break
        time.sleep(0.25)
    print(f"intervals={intervals}: status {status['partitions'][0]['status']} at +{time.time() - moved:.2f}s")
    assert status["status"] == "ERR"
    check_partition(status["partitions"][0], "STOP", (10, 10), 5, 1 / intervals, end_lag=0)


def test_allowed_lag(kafka, service, topics):
    topic = topics(3)
    kafka.produce(topic, {0: 20, 1: 8, 2: 10})
    svc = service(storage={"intervals": 4}, evaluator={"allowed-lag": 5})
    svc.wait_topic(topic, [20, 8, 10])
    group = unique("allowed")
    kafka.commit(group, topic, {0: 16, 1: 2, 2: 5})
    for partition, offset in ((0, 16), (1, 2), (2, 5)):
        svc.wait_window(group, topic, partition, [offset])
    time.sleep(GAP)
    kafka.commit(group, topic, {0: 16, 1: 2, 2: 5})
    for partition, offset in ((0, 16), (1, 2), (2, 5)):
        svc.wait_window(group, topic, partition, [offset, offset])
    status = evaluation(svc, group)
    assert status["status"] == "ERR" and status["totallag"] == 15
    parts = by_partition(status)
    check_partition(parts[(topic, 0)], "OK", (16, 16), 4, 0.5)
    check_partition(parts[(topic, 1)], "STALL", (2, 2), 6, 0.5)
    check_partition(parts[(topic, 2)], "OK", (5, 5), 5, 0.5)
    assert status["maxlag"]["partition"] == 1


def test_minimum_complete(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 4}, evaluator={"minimum-complete": 0.5})
    svc.wait_topic(topic, [10])
    group = unique("mincomplete")
    kafka.commit(group, topic, {0: 3})
    svc.wait_window(group, topic, 0, [3])
    time.sleep(2)
    status = evaluation(svc, group)
    assert status["status"] == "OK"
    check_partition(status["partitions"][0], "OK", (3, 3), 7, 0.25)
    kafka.commit(group, topic, {0: 3})
    svc.wait_window(group, topic, 0, [3, 3])
    time.sleep(1.2)  # outlive the cached first evaluation
    status = evaluation(svc, group)
    assert status["status"] == "ERR"
    check_partition(status["partitions"][0], "STALL", (3, 3), 7, 0.5)


def test_group_aggregation(kafka, service, topics):
    slow, steady = topics(1), topics(3)
    kafka.produce(slow, {0: 4})
    kafka.produce(steady, {0: 6, 1: 6, 2: 6})
    svc = service(storage={"intervals": 2})
    svc.wait_topic(slow, [4])
    svc.wait_topic(steady, [6, 6, 6])
    group = unique("aggregate")

    kafka.commit(group, slow, {0: 1})
    kafka.commit(group, steady, {0: 2, 1: 2})
    svc.wait_window(group, slow, 0, [1])
    svc.wait_window(group, steady, 1, [2])
    time.sleep(GAP)
    kafka.produce(slow, {0: 4})
    svc.wait_topic(slow, [8])
    kafka.commit(group, slow, {0: 2})
    kafka.commit(group, steady, {0: 4})
    svc.wait_window(group, slow, 0, [1, 2])
    svc.wait_window(group, steady, 0, [2, 4])
    status = evaluation(svc, group)
    parts = by_partition(status)
    assert set(parts) == {(slow, 0), (steady, 0), (steady, 1), (steady, 2)}
    check_partition(parts[(slow, 0)], "WARN", (1, 2), 6, 1, end_lag=6)
    check_partition(parts[(steady, 0)], "OK", (2, 4), 2, 1, end_lag=2)
    check_partition(parts[(steady, 1)], "STOP", (2, 2), 4, 0.5)
    check_partition(parts[(steady, 2)], "OK", None, 0, 0)
    assert status["status"] == "ERR"
    assert status["partition_count"] == 4 and status["totallag"] == 12
    assert number(status["complete"], 0.5)
    assert status["maxlag"]["topic"] == slow and status["maxlag"]["partition"] == 0
    filtered = evaluation(svc, group, "status")
    assert {(p["topic"], p["partition"]) for p in filtered["partitions"]} == {(slow, 0), (steady, 1)}
    assert len(filtered["partitions"]) == 2


def test_warning_only_group_and_lag_ties(kafka, service, topics):
    topic = topics(3)
    kafka.produce(topic, {0: 6, 1: 6, 2: 4})
    svc = service(storage={"intervals": 4})
    svc.wait_topic(topic, [6, 6, 4])
    tied, warned = unique("tied"), unique("warned")
    kafka.commit(tied, topic, {0: 6, 1: 6})
    kafka.commit(warned, topic, {2: 1})
    svc.wait_window(tied, topic, 1, [6])
    svc.wait_window(warned, topic, 2, [1])
    time.sleep(GAP)
    kafka.produce(topic, {2: 4})
    svc.wait_topic(topic, [6, 6, 8])
    kafka.commit(warned, topic, {2: 2})
    svc.wait_window(warned, topic, 2, [1, 2])
    status = evaluation(svc, warned)
    assert status["status"] == "WARN" and status["totallag"] == 6
    parts = by_partition(status)
    check_partition(parts[(topic, 2)], "WARN", (1, 2), 6, 0.5, end_lag=6)
    check_partition(parts[(topic, 0)], "OK", None, 0, 0)
    check_partition(parts[(topic, 1)], "OK", None, 0, 0)
    assert status["maxlag"]["partition"] == 2

    # Equal lags within one topic: the lowest partition is reported.
    status = evaluation(svc, tied)
    assert status["status"] == "OK" and status["totallag"] == 0
    assert status["maxlag"]["topic"] == topic and status["maxlag"]["partition"] == 0


def test_evaluation_cache(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 4}, evaluator={"expire-cache": 6})
    svc.wait_topic(topic, [10])
    known, late = unique("cached"), unique("late")

    kafka.commit(known, topic, {0: 2})
    svc.wait_window(known, topic, 0, [2])
    first = evaluation(svc, known)
    assert first["partitions"][0]["end"]["offset"] == 2
    assert svc.lag(late, status=404)["status"]["status"] == "NOTFOUND"
    queried = time.time()

    kafka.commit(known, topic, {0: 5})
    kafka.commit(late, topic, {0: 7})
    svc.wait_window(known, topic, 0, [2, 5])
    svc.wait_window(late, topic, 0, [7])
    assert time.time() < queried + 3.5, "too slow to observe the cache"

    # A successful evaluation is reused, for both views, until expire-cache has passed.
    for kind in ("lag", "status", "lag"):
        stale = evaluation(svc, known, kind)
        assert stale["maxlag"]["end"]["offset"] == 2 and stale["totallag"] == 8

    # A NOTFOUND result is answered once more after the group appears, then re-evaluated.
    assert svc.lag(late, status=404)["status"]["status"] == "NOTFOUND"
    time.sleep(1)
    found = evaluation(svc, late, "status")
    assert found["maxlag"]["end"]["offset"] == 7 and found["totallag"] == 3
    assert time.time() < queried + 5.5

    time.sleep(max(0.0, queried + 7 - time.time()))
    fresh = evaluation(svc, known)
    assert fresh["partitions"][0]["end"]["offset"] == 5 and fresh["totallag"] == 5
