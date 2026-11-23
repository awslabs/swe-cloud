"""Publishing and fan-out: routing by topic, filter and state, and destination message formats."""

import json
import time
from pathlib import Path

from hub import ENDPOINT, assert_quiet, error, ids, metadata_attribute, receive, recent, same, uid

FILTER_CASES = json.loads((Path(__file__).parent / "fixtures/filter_cases.json").read_text())


def test_sqs_message_body_and_metadata(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, id="dest-a")
    event_id = "ev-" + uid()
    raw = ('{"id":"%s","tenant_id":"%s","topic":"user.created","time":"2026-01-02T03:04:05Z","metadata":{"source":"probe"},'
           '"data": {"z": 1, "a": [1, 2.50, 1e3, true, null], "nested": {"k": "v\\u00e9", "empty": {}}, "amount": -5}}') % (event_id, tid)
    start = time.time()
    r = hub.request("POST", "/publish", raw=raw.encode())
    assert r.status == 202, r
    same(r.json, {"id": event_id, "duplicate": False, "destination_ids": ["dest-a"]})
    [message] = receive(url, 1)
    assert message["Body"] == '{"z":1,"a":[1,2.50,1e3,true,null],"nested":{"k":"v\\u00e9","empty":{}},"amount":-5}'
    meta = metadata_attribute(message)
    assert set(meta) == {"event-id", "topic", "timestamp", "source"}
    assert meta["event-id"] == event_id and meta["topic"] == "user.created" and meta["source"] == "probe"
    recent(meta["timestamp"], start)
    assert_quiet(url)


def test_metadata_precedence_and_millisecond_timestamp(make_hub, queues):
    hub = make_hub(DESTINATIONS_INCLUDE_MILLISECOND_TIMESTAMP="true")
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*", id="m", delivery_metadata={"app": "hub", "topic": "from-destination", "team": "x"})
    start = time.time()
    hub.publish({"id": "e1-" + uid(), "tenant_id": tid, "topic": "user.updated", "data": {"n": 1},
                 "metadata": {"team": "from-event", "event-id": "custom-id"}})
    hub.publish({"id": "e2-" + uid(), "tenant_id": tid, "topic": "user.deleted", "data": {"n": 2}})
    messages = sorted(receive(url, 2), key=lambda m: m["Body"])
    assert [m["Body"] for m in messages] == ['{"n":1}', '{"n":2}']
    first, second = (metadata_attribute(m) for m in messages)
    assert set(first) == {"event-id", "topic", "timestamp", "timestamp-ms", "app", "team"}
    assert (first["event-id"], first["topic"], first["app"], first["team"]) == ("custom-id", "from-destination", "hub", "from-event")
    assert second["topic"] == "from-destination" and second["team"] == "x" and second["event-id"].startswith("e2-")
    for meta in (first, second):
        recent(meta["timestamp-ms"], start)
        assert meta["timestamp-ms"].startswith(meta["timestamp"][:-1])


def test_eventbridge_envelope(make_hub, bus):
    hub = make_hub(DESTINATIONS_AWS_EVENTBRIDGE_SOURCE="hub.test")
    name, target = bus
    tid = hub.tenant()
    hub.ok("POST", f"/tenants/{tid}/destinations", {
        "id": "eb", "type": "aws_eventbridge", "topics": ["user.updated"],
        "config": {"event_bus_name": name, "region": "us-east-1", "endpoint": ENDPOINT}, "credentials": {"key": "test", "secret": "test"}},
        status=201)
    event_id = "eb-" + uid()
    start = time.time()
    hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.updated", "data": {"amount": 99.5, "tags": ["a"]},
                 "metadata": {"source": "billing"}})
    [message] = receive(target, 1, timeout=20)
    envelope = json.loads(message["Body"])
    assert envelope["source"] == "hub.test"
    assert envelope["detail-type"] == "user.updated"
    assert envelope["event-bus-name"] == name
    detail = envelope["detail"]
    assert set(detail) == {"data", "metadata"}
    same(detail["data"], {"amount": 99.5, "tags": ["a"]})
    meta = detail["metadata"]
    assert set(meta) == {"event-id", "topic", "timestamp", "source"}
    assert (meta["event-id"], meta["topic"], meta["source"]) == (event_id, "user.updated", "billing")
    recent(meta["timestamp"], start)
    attempts = hub.wait_attempts(tid, 1)
    assert [(a["status"], a["code"], a["destination_type"]) for a in attempts] == [("success", "OK", "aws_eventbridge")]
    with hub.pg() as conn:
        [(response,)] = conn.execute("select response_data from attempts where event_id=%s", (event_id,)).fetchall()
    stored = json.loads(response)
    assert set(stored) == {"detail_type", "event_bus_name", "event_id", "source"}
    assert (stored["detail_type"], stored["event_bus_name"], stored["source"]) == ("user.updated", name, "hub.test")
    assert stored["event_id"] == envelope["id"]


def test_eventbridge_default_source_and_missing_bus(hub, bus):
    name, target = bus
    tid = hub.tenant()
    for dest_id, bus_name in (("good", name), ("bad", "absent-" + uid())):
        hub.ok("POST", f"/tenants/{tid}/destinations", {
            "id": dest_id, "type": "aws_eventbridge", "topics": ["user.deleted"],
            "config": {"event_bus_name": bus_name, "region": "us-east-1", "endpoint": ENDPOINT},
            "credentials": {"key": "test", "secret": "test"}}, status=201)
    event_id = "ev-" + uid()
    out = hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.deleted", "data": {"x": 1}, "eligible_for_retry": False})
    assert ids(out["destination_ids"]) == ["bad", "good"]
    [message] = receive(target, 1, timeout=20)
    envelope = json.loads(message["Body"])
    assert envelope["source"] == "outpost" and envelope["detail-type"] == "user.deleted"
    attempts = {a["destination_id"]: a for a in hub.wait_attempts(tid, 2)}
    assert (attempts["good"]["status"], attempts["good"]["code"]) == ("success", "OK")
    assert (attempts["bad"]["status"], attempts["bad"]["code"]) == ("failed", "ERR")
    with hub.pg() as conn:
        [(response,)] = conn.execute("select response_data from attempts where destination_id='bad'").fetchall()
    stored = json.loads(response)
    assert stored["error_code"] == "InvalidArgument" and "EventBus not found" in stored["error"]


def test_routing_by_topic_filter_and_state(hub, queues):
    tid = hub.tenant()
    qa, qb, qd = queues.create(), queues.create(), queues.create()
    hub.sqs_destination(tid, qa, topics=["user.created"], id="a")
    hub.sqs_destination(tid, qb, topics="*", id="b", filter={"data": {"amount": {"$gte": 100}}})
    hub.sqs_destination(tid, qd, topics="*", id="d")
    hub.ok("PUT", f"/tenants/{tid}/destinations/d/disable")
    other = hub.tenant()
    qo = queues.create()
    hub.sqs_destination(other, qo, topics="*", id="a")

    def publish(topic, data, **extra):
        return ids(hub.publish({"tenant_id": tid, "topic": topic, "data": data, **extra})["destination_ids"])

    assert publish("user.created", {"amount": 5}) == ["a"]
    assert publish("user.created", {"amount": 150}) == ["a", "b"]
    assert publish("user.updated", {"amount": 100}) == ["b"]
    assert publish("user.updated", {"amount": 99.5}) == []
    assert publish("user.deleted", {"amount": "500"}) == []
    assert publish("user.updated", {"amount": 1000}, destination_id="a") == []
    assert publish("user.created", {"amount": 1000}, destination_id="a") == ["a"]
    assert publish("user.created", {"amount": 1000}, destination_id="d") == []
    assert publish("user.created", {"amount": 1000}, destination_id="missing") == []
    assert sorted(m["Body"] for m in receive(qa, 3)) == ['{"amount":1000}', '{"amount":150}', '{"amount":5}']
    assert sorted(m["Body"] for m in receive(qb, 2)) == ['{"amount":100}', '{"amount":150}']
    assert_quiet(qa)
    assert_quiet(qb, wait=1)
    assert_quiet(qd, wait=1)
    assert_quiet(qo, wait=1)

    hub.ok("PUT", f"/tenants/{tid}/destinations/d/enable")
    assert publish("user.deleted", {"amount": 1}) == ["d"]
    assert [m["Body"] for m in receive(qd, 1)] == ['{"amount":1}']
    hub.ok("PATCH", f"/tenants/{tid}/destinations/a", {"topics": ["user.deleted"]})
    assert publish("user.created", {"amount": 1}) == ["d"]
    assert publish("user.deleted", {"amount": 2}) == ["a", "d"]
    assert sorted(m["Body"] for m in receive(qd, 2)) == ['{"amount":1}', '{"amount":2}']
    hub.ok("DELETE", f"/tenants/{tid}/destinations/d")
    assert publish("user.deleted", {"amount": 3}) == ["a"]
    assert sorted(m["Body"] for m in receive(qa, 2)) == ['{"amount":2}', '{"amount":3}']
    moved = queues.create()
    hub.ok("PATCH", f"/tenants/{tid}/destinations/a", {"config": {"queue_url": moved}})
    assert publish("user.deleted", {"amount": 4}) == ["a"]
    assert [m["Body"] for m in receive(moved, 1)] == ['{"amount":4}']
    assert_quiet(qa)
    assert_quiet(qd, wait=1)


def test_filter_semantics(hub, queues):
    """Each case is published to a destination carrying the case's filter (cases adapted from the filter library tests)."""
    tid = hub.tenant()
    hub.sqs_destination(tid, queues.create(), topics="*", id="flt")
    mismatches = []
    for i, case in enumerate(FILTER_CASES):
        hub.ok("PATCH", f"/tenants/{tid}/destinations/flt", {"filter": case["filter"]})
        out = hub.publish({"id": f"f{i}-{uid()}-match", "tenant_id": tid, "topic": "user.created",
                           "metadata": {"source": "billing"}, "destination_id": "flt", "data": case["data"]})
        matched = out["destination_ids"] == ["flt"]
        if out["destination_ids"] not in ([], ["flt"]) or matched != case["match"]:
            mismatches.append((case["name"], out["destination_ids"], case["match"]))
    assert not mismatches, mismatches


def test_publish_validation(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*")
    rfc = 'invalid timestamp "yesterday": expected RFC3339 (e.g. 2024-01-15T09:30:00Z or 2024-01-15T09:30:00.123Z)'
    cases = [
        (None, b"{", 422, "invalid JSON", None),
        (None, b"", 422, "invalid JSON", None),
        ({"tenant_id": 1, "topic": "user.created", "data": {}}, None, 422, "invalid JSON", None),
        ({"tenant_id": tid, "topic": "user.created", "data": {}, "time": "yesterday"}, None, 422, rfc, None),
        ({"topic": "user.created", "data": {}}, None, 422, "validation error", ["tenant_id is required"]),
        ({"tenant_id": tid, "topic": "user.created"}, None, 422, "validation error", ["data is required"]),
        ({"tenant_id": tid, "topic": "user.created", "data": "text"}, None, 422, "validation error", ["data must be a valid JSON object"]),
        ({"tenant_id": tid, "topic": "user.created", "data": [1]}, None, 422, "validation error", ["data must be a valid JSON object"]),
        ({"tenant_id": tid, "topic": "user.created", "data": None}, None, 422, "validation error", ["data must be a valid JSON object"]),
        ({"tenant_id": tid, "data": {}}, None, 422, "validation error", ["topic is required"]),
        ({"tenant_id": tid, "topic": "", "data": {}}, None, 422, "validation error", ["topic is required"]),
        ({"tenant_id": tid, "topic": "nope", "data": {}}, None, 422, "validation error", ["topic is invalid"]),
        ({"tenant_id": tid, "topic": "user.created", "data": {}, "metadata": {"n": 1}}, None, 422, "invalid JSON", None),
        ({"tenant_id": tid, "topic": "user.created", "data": {}, "eligible_for_retry": "yes"}, None, 422, "invalid JSON", None),
    ]
    for body, raw, status, message, data in cases:
        try:
            error(hub.request("POST", "/publish", body, raw=raw), status, message, data)
        except AssertionError as e:
            raise AssertionError(f"{body or raw}: {e}") from None
    assert_quiet(url)
    same(hub.publish({"tenant_id": "absent-" + uid(), "topic": "user.created", "data": {"m": 1}})["destination_ids"], [])
    time.sleep(1.5)
    with hub.pg() as conn:
        assert conn.execute("select count(*) from events where tenant_id=%s", (tid,)).fetchone()[0] == 0


def test_duplicate_event_ids(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics=["user.created"], id="a")
    event_id = "dup-" + uid()
    body = {"id": event_id, "tenant_id": tid, "topic": "user.created", "data": {"v": 1}}
    same(hub.publish(body), {"id": event_id, "duplicate": False, "destination_ids": ["a"]})
    same(hub.publish({**body, "data": {"v": 2}}), {"id": event_id, "duplicate": True, "destination_ids": ["a"]})
    assert [m["Body"] for m in receive(url, 1)] == ['{"v":1}']
    # A republish that matches nothing is not reported as a duplicate.
    same(hub.publish({**body, "topic": "user.deleted"}), {"id": event_id, "duplicate": False, "destination_ids": []})
    assert_quiet(url)
    generated = hub.publish({"tenant_id": tid, "topic": "user.created", "data": {}})
    assert isinstance(generated["id"], str) and len(generated["id"]) == 36 and generated["id"].count("-") == 4
    assert [m["Body"] for m in receive(url, 1)] == ["{}"]
    [event] = [e for e in hub.wait_events(tid, 2) if e["id"] == event_id]
    same(event["data"], {"v": 1})
    assert event["topic"] == "user.created"


def test_topics_unset_and_wildcards(make_hub, queues):
    hub = make_hub(TOPICS=None, TOPICS_ALLOW_WILDCARDS="true")
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics=["user.*"], id="wc")
    hub.sqs_destination(tid, url, topics=["order.*.paid", "anything"], id="wc2")
    hub.sqs_destination(tid, url, topics="*", id="all")
    expected = {
        "user.created": ["all", "wc"], "user.x.y": ["all", "wc"], "users.created": ["all"],
        "order.eu.paid": ["all", "wc2"], "order.paid": ["all"], "anything": ["all", "wc2"], "whatever": ["all"],
    }
    for topic, want in expected.items():
        assert ids(hub.publish({"tenant_id": tid, "topic": topic, "data": {"t": topic}})["destination_ids"]) == want, topic
    assert ids(hub.publish({"tenant_id": tid, "data": {"t": "none"}})["destination_ids"]) == ["all", "wc", "wc2"]
    assert ids(hub.publish({"tenant_id": tid, "topic": "", "data": {"t": "empty"}})["destination_ids"]) == ["all", "wc", "wc2"]
    bodies = sorted(m["Body"] for m in receive(url, 17, timeout=20))
    assert len(bodies) == 17
    assert_quiet(url)
