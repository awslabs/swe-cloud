"""Stored commit windows per group/partition: decoding, bookkeeping, filters, expiry and removal."""

import time

from lagsvc import CLUSTER, is_int, unique

OFFSET_KEYS = {"offset", "timestamp", "observedAt", "lag"}
PARTITION_KEYS = {"offsets", "owner", "client_id", "current-lag"}


def now_ms():
    return int(time.time() * 1000)


def assert_entry(entry, offset, lag, earliest, latest):
    """One stored commit; times are only checked to lie within the test's own window."""
    assert set(entry) == OFFSET_KEYS, entry
    assert all(type(entry[key]) in (int, float) for key in OFFSET_KEYS), entry
    assert type(entry["offset"]) in (int, float) and entry["offset"] == offset, entry
    assert type(entry["lag"]) in (int, float) and entry["lag"] == lag, entry
    for key in ("timestamp", "observedAt"):
        assert is_int(entry[key]) and earliest - 2000 <= entry[key] <= latest + 2000, (key, entry, earliest, latest)


def test_commit_window_detail(kafka, service, topics):
    topic = topics(2)
    kafka.produce(topic, {0: 10, 1: 4})
    svc = service(storage={"intervals": 5})
    svc.wait_topic(topic, [10, 4])
    group = unique("detail")
    before = now_ms()
    kafka.commit(group, topic, {0: 4})
    svc.wait_window(group, topic, 0, [4])
    after = now_ms()

    body = svc.detail(group)
    assert set(body) == {"error", "message", "topics", "request"}
    assert body["error"] is False and body["message"] == "consumer detail returned"
    assert body["request"]["url"] == f"/v3/kafka/{CLUSTER}/consumer/{group}"
    assert list(body["topics"]) == [topic]
    first, second = body["topics"][topic]
    assert set(first) == PARTITION_KEYS and set(second) == PARTITION_KEYS
    assert first["offsets"][:4] == [None, None, None, None] and len(first["offsets"]) == 5
    assert_entry(first["offsets"][4], 4, 6, before, after)
    assert (first["owner"], first["client_id"], first["current-lag"]) == ("", "", 6)
    assert type(first["current-lag"]) in (int, float)
    assert second == {"offsets": [], "owner": "", "client_id": "", "current-lag": 0}

    # Stored lag is fixed at commit time; current lag follows the broker.
    kafka.produce(topic, {0: 5})
    svc.wait_topic(topic, [15, 4])
    first = svc.detail(group)["topics"][topic][0]
    assert first["offsets"][4]["lag"] == 6 and first["current-lag"] == 11

    kafka.commit(group, topic, {1: 4})
    svc.wait_window(group, topic, 1, [4])
    first, second = svc.detail(group)["topics"][topic]
    assert [entry and entry["offset"] for entry in first["offsets"]] == [None, None, None, None, 4]
    assert second["offsets"][:4] == [None] * 4 and second["offsets"][4]["lag"] == 0
    assert second["current-lag"] == 0

    # A commit beyond the broker's end offset has no lag.
    ahead = unique("ahead")
    kafka.commit(ahead, topic, {0: 50})
    window = svc.wait_window(ahead, topic, 0, [50])
    assert type(window[0]["lag"]) in (int, float) and window[0]["lag"] == 0 and svc.detail(ahead)["topics"][topic][0]["current-lag"] == 0
    status = svc.lag(ahead)["status"]
    assert status["status"] == "OK" and status["totallag"] == 0


def test_window_keeps_newest_commits(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 30})
    svc = service(storage={"intervals": 3})
    svc.wait_topic(topic, [30])
    group = unique("ring")
    for offset in (1, 2, 3, 4, 5):
        kafka.commit(group, topic, {0: offset})
        svc.wait_json(f"/v3/kafka/{CLUSTER}/consumer/{group}", lambda body, o=offset: body["topics"][topic][0]["offsets"][-1]["offset"] == o)
    slots = svc.detail(group)["topics"][topic][0]["offsets"]
    assert [entry["offset"] for entry in slots] == [3, 4, 5]
    assert [entry["lag"] for entry in slots] == [27, 26, 25]
    stamps = [entry["timestamp"] for entry in slots]
    assert stamps == sorted(stamps)


def test_min_distance_merges_close_commits(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 10})
    svc = service(storage={"intervals": 4, "min-distance": 3})
    svc.wait_topic(topic, [10])
    group = unique("merge")
    start = time.time()
    kafka.commit(group, topic, {0: 1})
    first_stamp = svc.wait_window(group, topic, 0, [1])[0]["timestamp"]
    kafka.commit(group, topic, {0: 2})
    svc.wait_window(group, topic, 0, [2])
    time.sleep(max(0.0, start + 1.2 - time.time()))
    kafka.commit(group, topic, {0: 3})
    merged = svc.wait_window(group, topic, 0, [3])
    assert merged[0]["timestamp"] == first_stamp and merged[0]["lag"] == 7
    time.sleep(max(0.0, start + 4.5 - time.time()))
    kafka.commit(group, topic, {0: 5})
    window = svc.wait_window(group, topic, 0, [3, 5])
    assert window[0]["timestamp"] == first_stamp
    assert window[1]["timestamp"] - first_stamp >= 3000
    assert [entry["lag"] for entry in window] == [7, 5]
    slots = svc.detail(group)["topics"][topic][0]["offsets"]
    assert slots[:2] == [None, None]


def test_consumer_list(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 3})
    svc = service()
    svc.wait_topic(topic, [3])
    groups = [unique("listed") for _ in range(3)]
    for group in groups:
        kafka.commit(group, topic, {0: 1})
    for group in groups:
        svc.wait_window(group, topic, 0, [1])
    body = svc.json(f"/v3/kafka/{CLUSTER}/consumer")
    assert set(body) == {"error", "message", "consumers", "request"}
    assert body["error"] is False and body["message"] == "consumer list returned"
    assert len(body["consumers"]) == len(set(body["consumers"]))
    assert set(groups) <= set(body["consumers"])
    assert f"burrow-{CLUSTER}" in body["consumers"]


def test_group_filters(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 8})
    tag = unique("f")
    keep_a, keep_b, skipped, other = f"keep-a-{tag}", f"keep-b-{tag}", f"skip-{tag}", f"other-{tag}"

    first = service(consumer={"group-denylist": "^skip-"}, storage={"group-allowlist": "^(keep|skip)-"})
    first.wait_topic(topic, [8])
    for group in (skipped, other, keep_a):
        kafka.commit(group, topic, {0: 2})
    first.wait_window(keep_a, topic, 0, [2])
    listed = first.consumers()
    assert keep_a in listed and skipped not in listed and other not in listed
    assert f"burrow-{CLUSTER}" not in listed  # the storage allowlist applies to the service's own group too
    for group in (skipped, other):
        assert first.get(f"/v3/kafka/{CLUSTER}/consumer/{group}").status_code == 404
    first.stop()

    # A consumer-module allowlist filters decoded commits only; the own progress group is unaffected.
    second = service(consumer={"group-allowlist": f"^keep-.*-{tag}$"}, storage={"group-denylist": "^keep-b-"})
    second.wait_topic(topic, [8])
    kafka.commit(keep_b, topic, {0: 3})
    kafka.commit(other, topic, {0: 3})
    kafka.commit(keep_a, topic, {0: 4})
    second.wait_window(keep_a, topic, 0, [2, 4])
    listed = second.consumers()
    assert keep_a in listed and f"burrow-{CLUSTER}" in listed
    assert keep_b not in listed and other not in listed and skipped not in listed


def test_http_deletion(kafka, service, topics):
    left, right = topics(1), topics(1)
    kafka.produce(left, {0: 5})
    kafka.produce(right, {0: 5})
    svc = service()
    svc.wait_topic(left, [5])
    svc.wait_topic(right, [5])
    two, whole, bystander = unique("two"), unique("whole"), unique("bystander")
    kafka.commit(two, left, {0: 1})
    kafka.commit(two, right, {0: 2})
    kafka.commit(whole, left, {0: 3})
    kafka.commit(bystander, left, {0: 4})
    svc.wait_window(two, right, 0, [2])
    svc.wait_window(two, left, 0, [1])
    svc.wait_window(whole, left, 0, [3])
    svc.wait_window(bystander, left, 0, [4])

    path = f"/v3/kafka/{CLUSTER}/consumer/{two}/topic/{left}"
    response = svc.request("DELETE", path)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"error", "message", "request"}
    assert body["error"] is False and body["message"] == "consumer group removed" and body["request"]["url"] == path
    assert list(svc.detail(two)["topics"]) == [right]
    assert two in svc.consumers()

    assert svc.request("DELETE", f"/v3/kafka/{CLUSTER}/consumer/{two}/topic/{right}").status_code == 200
    assert svc.get(f"/v3/kafka/{CLUSTER}/consumer/{two}").status_code == 404
    assert two not in svc.consumers()

    response = svc.request("DELETE", f"/v3/kafka/{CLUSTER}/consumer/{whole}")
    assert response.status_code == 200 and response.json()["message"] == "consumer group removed"
    assert svc.get(f"/v3/kafka/{CLUSTER}/consumer/{whole}").status_code == 404
    absent = svc.request("DELETE", f"/v3/kafka/{CLUSTER}/consumer/{unique('never')}")
    assert absent.status_code == 200 and absent.json()["error"] is False

    listed = svc.consumers()
    assert bystander in listed and whole not in listed
    assert [entry["offset"] for entry in svc.window(bystander, left, 0)] == [4]

    # Removal only forgets stored state; a later commit starts a fresh window.
    kafka.commit(whole, left, {0: 5})
    svc.wait_window(whole, left, 0, [5])
    assert whole in svc.consumers()


def test_broker_group_deletion_removes_group(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 6})
    svc = service()
    svc.wait_topic(topic, [6])
    doomed, kept = unique("doomed"), unique("kept")
    kafka.commit(doomed, topic, {0: 2})
    kafka.commit(kept, topic, {0: 3})
    svc.wait_window(doomed, topic, 0, [2])
    svc.wait_window(kept, topic, 0, [3])
    kafka.delete_group(doomed)
    svc.wait_json(f"/v3/kafka/{CLUSTER}/consumer/{doomed}", lambda body: True, status=404)
    listed = svc.consumers()
    assert doomed not in listed and kept in listed
    assert [entry["offset"] for entry in svc.window(kept, topic, 0)] == [3]


def test_service_reports_its_own_progress(kafka, service):
    svc = service()
    own = f"burrow-{CLUSTER}"
    metadata = kafka.admin.list_topics("__consumer_offsets", timeout=10).topics["__consumer_offsets"]
    count = len(metadata.partitions)

    def caught_up(body):
        partitions = body["topics"].get("__consumer_offsets", [])
        if len(partitions) != count:
            return False
        ends = kafka.end_offsets("__consumer_offsets", count)
        for partition, item in enumerate(partitions):
            stored = [entry for entry in item["offsets"] if entry is not None]
            if ends[partition] == 0:
                if stored:
                    return False
            elif not stored or stored[-1]["offset"] != ends[partition] or item["current-lag"] != 0:
                return False
        return True

    body = svc.wait_json(f"/v3/kafka/{CLUSTER}/consumer/{own}", caught_up)
    assert list(body["topics"]) == ["__consumer_offsets"]
    assert all(item["owner"] == "" and item["client_id"] == "" for item in body["topics"]["__consumer_offsets"])
    status = svc.lag(own)["status"]
    assert status["status"] == "OK" and status["totallag"] == 0 and status["partition_count"] == count


def test_expired_group_is_purged_when_read(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 5})
    svc = service(storage={"expire-group": 4})
    svc.wait_topic(topic, [5])
    group = unique("expiring")
    kafka.commit(group, topic, {0: 2})
    svc.wait_window(group, topic, 0, [2])
    committed = time.time()
    time.sleep(max(0.0, committed + 6.5 - time.time()))
    assert group in svc.consumers(), "expiry is applied when the group is read, not by listing"
    missing = svc.json(f"/v3/kafka/{CLUSTER}/consumer/{group}", status=404)
    assert missing["error"] is True and missing["message"] == "cluster or consumer not found"
    assert group not in svc.consumers()
    assert svc.lag(group, status=404)["status"]["status"] == "NOTFOUND"

    kafka.commit(group, topic, {0: 3})
    svc.wait_window(group, topic, 0, [3])
    assert group in svc.consumers()
