"""Broker polling: topic lists, end offsets and topic-to-group lookups."""

from lagsvc import CLUSTER, unique


def test_topic_offsets_follow_the_broker(kafka, service, topics):
    topic = topics(3)
    kafka.produce(topic, {0: 10, 1: 5})
    assert kafka.end_offsets(topic, 3) == [10, 5, 0]
    svc = service()
    body = svc.wait_topic(topic, [10, 5, 0])
    assert set(body) == {"error", "message", "offsets", "request"}
    assert body["error"] is False and body["message"] == "topic offsets returned"
    assert body["request"]["url"] == f"/v3/kafka/{CLUSTER}/topic/{topic}"

    listing = svc.json(f"/v3/kafka/{CLUSTER}/topic")
    assert set(listing) == {"error", "message", "topics", "request"}
    assert listing["message"] == "topic list returned"
    assert topic in listing["topics"] and "__consumer_offsets" in listing["topics"]
    assert len(listing["topics"]) == len(set(listing["topics"]))

    kafka.produce(topic, {1: 2, 2: 7})
    assert kafka.end_offsets(topic, 3) == [10, 7, 7]
    svc.wait_topic(topic, [10, 7, 7])


def test_topics_created_and_deleted_while_running(kafka, service, topics):
    svc = service()
    topic = topics(2)
    kafka.produce(topic, {0: 3, 1: 1})
    svc.wait_topic(topic, [3, 1])
    assert topic in svc.json(f"/v3/kafka/{CLUSTER}/topic")["topics"]

    kafka.delete_topic(topic)
    svc.wait_json(f"/v3/kafka/{CLUSTER}/topic/{topic}", lambda body: body["error"] is True, status=404)
    assert topic not in svc.json(f"/v3/kafka/{CLUSTER}/topic")["topics"]


def test_consumers_of_topic(kafka, service, topics):
    first, second = topics(1), topics(2)
    kafka.produce(first, {0: 5})
    kafka.produce(second, {0: 5, 1: 5})
    svc = service()
    svc.wait_topic(first, [5])
    svc.wait_topic(second, [5, 5])
    both, only_second = unique("both"), unique("second")
    kafka.commit(both, first, {0: 1})
    kafka.commit(both, second, {1: 2})
    kafka.commit(only_second, second, {0: 4})
    svc.wait_window(both, second, 1, [2])
    svc.wait_window(only_second, second, 0, [4])
    svc.wait_window(both, first, 0, [1])

    body = svc.json(f"/v3/kafka/{CLUSTER}/topic/{first}/consumers")
    assert set(body) == {"error", "message", "consumers", "request"}
    assert body["error"] is False and body["message"] == "consumers of topic returned"
    assert body["consumers"] == [both]
    assert sorted(svc.json(f"/v3/kafka/{CLUSTER}/topic/{second}/consumers")["consumers"]) == sorted([both, only_second])
    assert svc.json(f"/v3/kafka/{CLUSTER}/topic/{unique('unused')}/consumers")["consumers"] == []
    missing = svc.json(f"/v3/kafka/nowhere/topic/{first}/consumers", status=404)
    assert missing["error"] is True and missing["message"] == "cluster not found"


def test_deleted_topic_leaves_group_topics(kafka, service, topics):
    kept, doomed = topics(2), topics(1)
    kafka.produce(kept, {0: 6, 1: 6})
    kafka.produce(doomed, {0: 4})
    svc = service()
    svc.wait_topic(kept, [6, 6])
    svc.wait_topic(doomed, [4])
    mixed, single = unique("mixed"), unique("single")
    kafka.commit(mixed, kept, {0: 2})
    kafka.commit(mixed, doomed, {0: 1})
    kafka.commit(single, doomed, {0: 3})
    svc.wait_window(mixed, doomed, 0, [1])
    svc.wait_window(mixed, kept, 0, [2])
    svc.wait_window(single, doomed, 0, [3])

    kafka.delete_topic(doomed)
    svc.wait_json(f"/v3/kafka/{CLUSTER}/consumer/{mixed}", lambda body: doomed not in body["topics"])
    topics_left = svc.detail(mixed)["topics"]
    assert list(topics_left) == [kept]
    assert [entry and entry["offset"] for entry in topics_left[kept][0]["offsets"]] == [None, None, None, None, 2]
    assert topics_left[kept][1]["offsets"] == []

    # A group whose only topic disappeared is still listed, with no topics left to evaluate.
    detail = svc.detail(single)
    assert detail["topics"] == {}
    assert single in svc.consumers()
    status = svc.lag(single)["status"]
    assert status["status"] == "OK" and status["partitions"] == [] and status["partition_count"] == 0
    assert status["maxlag"] is None and status["totallag"] == 0
    assert type(status["complete"]) in (int, float) and status["complete"] == 0
