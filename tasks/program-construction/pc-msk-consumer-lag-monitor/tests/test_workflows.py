"""Sequences across restarts and polling: history replay, start position, expiry and late topics."""

import time

from lagsvc import CLUSTER, unique


def test_history_is_replayed_at_startup(kafka, service, topics):
    topic = topics(2)
    kafka.produce(topic, {0: 10, 1: 10})
    group = unique("history")
    kafka.commit(group, topic, {0: 3})
    time.sleep(1.2)
    kafka.commit(group, topic, {0: 6, 1: 10})
    kafka.produce(topic, {0: 10})

    svc = service(storage={"intervals": 4})
    window = svc.wait_window(group, topic, 0, [3, 6])
    # Lags of replayed commits are measured against the broker offsets known at replay time.
    assert [entry["lag"] for entry in window] == [17, 14]
    assert all(type(entry["lag"]) in (int, float) for entry in window)
    assert window[1]["timestamp"] - window[0]["timestamp"] >= 1000
    assert [entry["offset"] for entry in svc.window(group, topic, 1)] == [10]
    assert group in svc.consumers()
    assert svc.json(f"/v3/kafka/{CLUSTER}/topic/{topic}")["offsets"] == [20, 10]
    detail = svc.detail(group)["topics"][topic]
    assert detail[0]["current-lag"] == 14 and detail[1]["current-lag"] == 0


def test_start_position(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 9})
    earlier = unique("earlier")
    kafka.commit(earlier, topic, {0: 2})

    latest = service(consumer={"start-latest": True})
    latest.wait_topic(topic, [9])
    later = unique("later")
    kafka.commit(later, topic, {0: 4})
    latest.wait_window(later, topic, 0, [4])
    assert latest.get(f"/v3/kafka/{CLUSTER}/consumer/{earlier}").status_code == 404
    assert earlier not in latest.consumers()
    assert latest.json(f"/v3/config/consumer/{CLUSTER}")["module"]["start-latest"] is True
    latest.stop()

    backfill = service(consumer={"start-latest": True, "backfill-earliest": True})
    assert [entry["offset"] for entry in backfill.wait_window(earlier, topic, 0, [2])] == [2]
    assert [entry["offset"] for entry in backfill.wait_window(later, topic, 0, [4])] == [4]
    newest = unique("newest")
    kafka.commit(newest, topic, {0: 5})
    backfill.wait_window(newest, topic, 0, [5])
    assert {earlier, later, newest} <= set(backfill.consumers())


def test_replay_drops_commits_older_than_expiry(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 6})
    stale, fresh, mixed = unique("stale"), unique("fresh"), unique("mixed")
    kafka.commit(stale, topic, {0: 1})
    kafka.commit(mixed, topic, {0: 2})
    time.sleep(9)
    kafka.commit(mixed, topic, {0: 3})
    kafka.commit(fresh, topic, {0: 4})

    svc = service(storage={"expire-group": 8})
    svc.wait_window(fresh, topic, 0, [4])
    assert [entry["offset"] for entry in svc.wait_window(mixed, topic, 0, [3])] == [3]
    assert stale not in svc.consumers()
    assert svc.get(f"/v3/kafka/{CLUSTER}/consumer/{stale}").status_code == 404


def test_commit_before_topic_is_known_is_dropped(kafka, service, topics):
    svc = service(cluster={"topic-refresh": 8})
    started = time.time()
    topic = topics(1)
    kafka.produce(topic, {0: 5})
    group = unique("early")
    kafka.commit(group, topic, {0: 2})
    assert time.time() - started < 6, "topic setup too slow for this scenario"
    time.sleep(1.5)
    assert topic not in svc.json(f"/v3/kafka/{CLUSTER}/topic")["topics"]
    assert svc.get(f"/v3/kafka/{CLUSTER}/consumer/{group}").status_code == 404

    svc.wait_topic(topic, [5], timeout=30)
    time.sleep(1.5)
    assert svc.get(f"/v3/kafka/{CLUSTER}/consumer/{group}").status_code == 404
    assert group not in svc.consumers()

    kafka.commit(group, topic, {0: 4})
    window = svc.wait_window(group, topic, 0, [4])
    assert type(window[0]["lag"]) in (int, float) and window[0]["lag"] == 1


def test_group_lifecycle(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 3})
    svc.wait_topic(topic, [10])
    group = unique("lifecycle")
    path = f"/v3/kafka/{CLUSTER}/consumer/{group}/lag"

    kafka.commit(group, topic, {0: 10})
    svc.wait_window(group, topic, 0, [10])
    assert svc.json(path)["status"]["status"] == "OK"

    kafka.produce(topic, {0: 6})
    svc.wait_topic(topic, [16])
    time.sleep(1.5)
    kafka.commit(group, topic, {0: 12})
    svc.wait_window(group, topic, 0, [10, 12])
    time.sleep(1.1)
    body = svc.json(path)["status"]
    assert body["status"] == "OK" and body["totallag"] == 4 and body["partitions"][0]["status"] == "OK"
    time.sleep(0.4)
    kafka.commit(group, topic, {0: 9})
    window = svc.wait_window(group, topic, 0, [10, 12, 9])
    assert [entry["lag"] for entry in window] == [0, 4, 7]
    time.sleep(1.1)
    body = svc.json(path)["status"]
    assert body["status"] == "ERR" and body["partitions"][0]["status"] == "REWIND"

    assert svc.request("DELETE", f"/v3/kafka/{CLUSTER}/consumer/{group}").status_code == 200
    assert svc.get(f"/v3/kafka/{CLUSTER}/consumer/{group}").status_code == 404
    svc.wait_json(path, lambda body: body["status"]["status"] == "NOTFOUND", status=404)
    kafka.commit(group, topic, {0: 16})
    svc.wait_window(group, topic, 0, [16])
    time.sleep(1.2)
    body = svc.json(path)["status"]
    assert body["status"] == "OK" and body["totallag"] == 0
    assert body["partitions"][0]["start"]["offset"] == 16 and body["partitions"][0]["end"]["offset"] == 16
