"""Destination management: validation, display, partial updates, enable/disable, deletion and storage."""

import json
import time

import pytest

from hub import CREDS, ENDPOINT, error, millis, model, recent, same, same_model, stored_number, uid


def dest_hash(hub, tenant, dest_id):
    raw = hub.engines.valkey().hgetall(hub.key(f"tenant:{{{tenant}}}:destination:{dest_id}"))
    return {k.decode(): v for k, v in raw.items()}


def summary(hub, tenant):
    raw = hub.engines.valkey().hgetall(hub.key(f"tenant:{{{tenant}}}:destinations"))
    return {k.decode(): json.loads(v) for k, v in raw.items()}


def sqs_view(tenant, dest_id, queue_url, topics, credentials=None, **extra):
    view = {"id": dest_id, "tenant_id": tenant, "type": "aws_sqs", "topics": topics,
            "config": {"endpoint": ENDPOINT, "queue_url": queue_url},
            "credentials": credentials or {"key": "****", "secret": "****"}, "target": queue_url}
    view.update(extra)
    return view


TIMES = ("created_at", "updated_at", "disabled_at")


def test_create_sqs_destination_and_storage(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    start = time.time()
    created = hub.request("POST", f"/tenants/{tid}/destinations", {
        "type": "aws_sqs", "topics": ["user.created", "user.updated"], "config": {"queue_url": url, "endpoint": ENDPOINT},
        "credentials": {"key": "AKIAEXAMPLEKEY123", "secret": "0123456789", "session": "sess-token-value"},
        "metadata": {"team": "billing"}, "delivery_metadata": {"app": "hub"}})
    assert created.status == 201, created
    dest = created.json
    assert isinstance(dest["id"], str) and dest["id"]
    model(dest, sqs_view(tid, dest["id"], url, ["user.created", "user.updated"],
                         credentials={"key": "AKIA*************", "secret": "0123******", "session": "sess-token-value"},
                         metadata={"team": "billing"}, delivery_metadata={"app": "hub"}), times=TIMES)
    assert dest.get("disabled_at") is None
    recent(dest["created_at"], start)
    assert millis(dest["created_at"]) == millis(dest["updated_at"])

    fetched = hub.ok("GET", f"/tenants/{tid}/destinations/{dest['id']}")
    model(fetched, {k: v for k, v in dest.items() if k not in TIMES}, times=TIMES)
    assert millis(fetched["created_at"]) == millis(dest["created_at"])

    record = dest_hash(hub, tid, dest["id"])
    text = {k: v.decode() for k, v in record.items() if k not in ("credentials", "delivery_metadata")}
    assert set(record) == {"id", "entity", "type", "topics", "config", "credentials", "created_at", "updated_at",
                           "metadata", "delivery_metadata"}
    assert text["id"] == dest["id"] and text["entity"] == "destination" and text["type"] == "aws_sqs"
    assert text["topics"] == "user.created,user.updated"
    same(json.loads(text["config"]), {"endpoint": ENDPOINT, "queue_url": url})
    same(json.loads(text["metadata"]), {"team": "billing"})
    assert stored_number(text["created_at"]) == stored_number(text["updated_at"]) == millis(dest["created_at"])
    for sealed in ("credentials", "delivery_metadata"):
        assert b"0123456789" not in record[sealed] and b"AKIAEXAMPLE" not in record[sealed] and b"hub" not in record[sealed]
    same(summary(hub, tid), {dest["id"]: {"id": dest["id"], "type": "aws_sqs", "topics": ["user.created", "user.updated"], "disabled": False}})
    model(hub.ok("GET", f"/tenants/{tid}"), {"id": tid, "destinations_count": 1, "topics": ["user.created", "user.updated"]},
          times=("created_at", "updated_at"))


def test_create_eventbridge_destination(hub):
    tid = hub.tenant()
    named = hub.ok("POST", f"/tenants/{tid}/destinations", {
        "id": "eb-named", "type": "aws_eventbridge", "topics": "*",
        "config": {"event_bus_name": "orders", "region": "eu-west-1", "endpoint": ENDPOINT},
        "credentials": {"key": "k", "secret": "secret-value-long"}}, status=201)
    model(named, {"id": "eb-named", "tenant_id": tid, "type": "aws_eventbridge", "topics": ["*"],
                  "config": {"endpoint": ENDPOINT, "event_bus_name": "orders", "region": "eu-west-1"},
                  "credentials": {"key": "*", "secret": "secr*************"}, "target": "orders in eu-west-1",
                  "target_url": "https://eu-west-1.console.aws.amazon.com/events/home?region=eu-west-1#/eventbus/orders"}, times=TIMES)
    default = hub.ok("POST", f"/tenants/{tid}/destinations", {
        "id": "eb-default", "type": "aws_eventbridge", "topics": ["user.deleted"], "config": {"region": "us-east-1"},
        "credentials": {"key": "kk", "secret": "ss"}}, status=201)
    model(default, {"id": "eb-default", "tenant_id": tid, "type": "aws_eventbridge", "topics": ["user.deleted"],
                    "config": {"region": "us-east-1"}, "credentials": {"key": "**", "secret": "**"},
                    "target": "default in us-east-1",
                    "target_url": "https://us-east-1.console.aws.amazon.com/events/home?region=us-east-1#/eventbus/default"}, times=TIMES)
    same(summary(hub, tid), {
        "eb-named": {"id": "eb-named", "type": "aws_eventbridge", "topics": ["*"], "disabled": False},
        "eb-default": {"id": "eb-default", "type": "aws_eventbridge", "topics": ["user.deleted"], "disabled": False},
    })
    model(hub.ok("GET", f"/tenants/{tid}"), {"id": tid, "destinations_count": 2, "topics": ["*"]}, times=("created_at", "updated_at"))


CREATE_ERRORS = [
    ("type missing", {"type": None}, 422, "validation error", ["type is required"]),
    ("topics missing", {"topics": None}, 422, "validation error", ["topics is required"]),
    ("topics empty", {"topics": []}, 422, "validation failed: invalid topics", None),
    ("topic not allowed", {"topics": ["nope"]}, 422, "validation failed: invalid topics", None),
    ("topics number", {"topics": 1}, 422, "validation failed: invalid topics format", None),
    ("topics plain string", {"topics": "user.created"}, 422, "validation failed: invalid topics format", None),
    ("wildcard mixed", {"topics": ["*", "user.created"]}, 422, "validation failed: invalid topics", None),
    ("unknown type", {"type": "nope"}, 422, "no provider registered for destination type: nope", None),
    ("config missing", {"config": None}, 422, "validation error", ["config.queue_url is required"]),
    ("queue url missing", {"config": {"endpoint": ENDPOINT}}, 422, "validation error", ["config.queue_url is required"]),
    ("queue url pattern", {"config": {"queue_url": "not a url"}}, 422, "validation error", ["config.queue_url failed pattern validation"]),
    ("endpoint pattern", {"config": {"queue_url": "http://localhost:4566/000000000000/q", "endpoint": "not a url"}}, 422,
     "validation error", ["config.endpoint failed pattern validation"]),
    ("config wrong type", {"config": "text"}, 422, "invalid JSON", None),
    ("credentials missing", {"credentials": None}, 422, "validation error", ["credentials.key is required", "credentials.secret is required"]),
    ("secret missing", {"credentials": {"key": "k"}}, 422, "validation error", ["credentials.secret is required"]),
    ("key missing", {"credentials": {"secret": "s"}}, 422, "validation error", ["credentials.key is required"]),
    ("created_at future", {"created_at": "2999-01-01T00:00:00Z"}, 422, "created_at cannot be in the future", None),
    ("created_at format", {"created_at": "yesterday"}, 422,
     'invalid timestamp "yesterday": expected RFC3339 (e.g. 2024-01-15T09:30:00Z or 2024-01-15T09:30:00.123Z)', None),
    ("eventbridge region", {"type": "aws_eventbridge", "config": {"event_bus_name": "b"}}, 422, "validation error", ["config.region is required"]),
    ("eventbridge endpoint", {"type": "aws_eventbridge", "config": {"region": "us-east-1", "endpoint": "ftp://x"}}, 422,
     "validation error", ["config.endpoint failed pattern validation"]),
    ("eventbridge credentials", {"type": "aws_eventbridge", "config": {"region": "us-east-1"}, "credentials": {}}, 422,
     "validation error", ["credentials.key is required", "credentials.secret is required"]),
]


def test_create_validation_leaves_no_destination(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    base = {"type": "aws_sqs", "topics": ["user.created"], "config": {"queue_url": url, "endpoint": ENDPOINT}, "credentials": dict(CREDS)}
    error(hub.request("POST", f"/tenants/{tid}/destinations", raw=b"{"), 422, "invalid JSON")
    error(hub.request("POST", f"/tenants/{tid}/destinations", raw=b""), 422, "invalid JSON")
    for name, change, status, message, data in CREATE_ERRORS:
        body = dict(base)
        for k, v in change.items():
            if v is None:
                body.pop(k)
            else:
                body[k] = v
        r = hub.request("POST", f"/tenants/{tid}/destinations", body)
        try:
            error(r, status, message, data)
        except AssertionError as e:
            raise AssertionError(f"{name}: {e}") from None
    assert hub.ok("GET", f"/tenants/{tid}/destinations") == []
    assert summary(hub, tid) == {}
    assert sorted(k.decode() for k in hub.engines.valkey().scan_iter(match=hub.key(f"tenant:{{{tid}}}:*"))) == \
        [hub.key(f"tenant:{{{tid}}}:tenant")]
    error(hub.request("POST", f"/tenants/t-{uid()}/destinations", base), 404, "tenant not found")


def test_duplicate_id_and_tenant_limit(make_hub, queues):
    hub = make_hub(MAX_DESTINATIONS_PER_TENANT=3)
    tid = hub.tenant()
    url = queues.create()
    first = hub.sqs_destination(tid, url, id="dup")
    error(hub.request("POST", f"/tenants/{tid}/destinations", {
        "id": "dup", "type": "aws_sqs", "topics": "*", "config": {"queue_url": queues.create()}, "credentials": dict(CREDS)}),
        400, "destination already exists")
    model(hub.ok("GET", f"/tenants/{tid}/destinations/dup"), {k: v for k, v in first.items() if k not in TIMES}, times=TIMES)
    other = hub.tenant()
    hub.sqs_destination(other, url, id="dup")

    hub.sqs_destination(tid, url, id="second")
    hub.sqs_destination(tid, url, id="third")
    error(hub.request("POST", f"/tenants/{tid}/destinations", {
        "id": "fourth", "type": "aws_sqs", "topics": "*", "config": {"queue_url": url}, "credentials": dict(CREDS)}),
        400, "maximum number of destinations per tenant reached")
    assert sorted(summary(hub, tid)) == ["dup", "second", "third"]
    hub.ok("DELETE", f"/tenants/{tid}/destinations/second")
    hub.sqs_destination(tid, url, id="fourth")
    assert sorted(summary(hub, tid)) == ["dup", "fourth", "third"]
    assert hub.ok("GET", f"/tenants/{tid}")["destinations_count"] == 3


def test_list_destinations(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    zeta = hub.sqs_destination(tid, url, topics="*", id="zeta")
    time.sleep(0.01)
    alpha = hub.ok("POST", f"/tenants/{tid}/destinations", {
        "id": "alpha", "type": "aws_eventbridge", "topics": ["user.updated"], "config": {"region": "us-east-1"},
        "credentials": dict(CREDS)}, status=201)
    time.sleep(0.01)
    mid = hub.sqs_destination(tid, url, topics=["user.deleted"], id="mid")
    listed = hub.ok("GET", f"/tenants/{tid}/destinations")
    assert [d["id"] for d in listed] == ["zeta", "alpha", "mid"]
    for got, want in zip(listed, (zeta, alpha, mid)):
        model(got, {k: v for k, v in want.items() if k not in TIMES}, times=TIMES)
    assert [d["id"] for d in hub.ok("GET", f"/tenants/{tid}/destinations?type=aws_eventbridge")] == ["alpha"]
    assert [d["id"] for d in hub.ok("GET", f"/tenants/{tid}/destinations?type=aws_sqs")] == ["zeta", "mid"]
    assert [d["id"] for d in hub.ok("GET", f"/tenants/{tid}/destinations?topics=user.deleted")] == ["zeta", "mid"]
    error(hub.request("GET", f"/tenants/{tid}/destinations/missing"), 404, "destination not found")


def test_patch_merges_fields(hub, queues):
    tid = hub.tenant()
    url, url2 = queues.create(), queues.create()
    dest = hub.sqs_destination(tid, url, id="p", metadata={"team": "a"}, delivery_metadata={"app": "hub"})
    path = f"/tenants/{tid}/destinations/p"

    time.sleep(0.01)
    topics = hub.ok("PATCH", path, {"topics": ["user.updated", "user.deleted"]})
    model(topics, sqs_view(tid, "p", url, ["user.updated", "user.deleted"], metadata={"team": "a"}, delivery_metadata={"app": "hub"}), times=TIMES)
    assert millis(topics["created_at"]) == millis(dest["created_at"])
    assert millis(topics["updated_at"]) > millis(dest["created_at"])

    merged = hub.ok("PATCH", path, {"config": {"queue_url": url2}, "credentials": {"key": "newkey"},
                                    "metadata": {"z": "1"}, "delivery_metadata": {"env": "test"}})
    model(merged, sqs_view(tid, "p", url2, ["user.updated", "user.deleted"], credentials={"key": "******", "secret": "****"},
                           metadata={"team": "a", "z": "1"}, delivery_metadata={"app": "hub", "env": "test"}), times=TIMES)
    filtered = hub.ok("PATCH", path, {"filter": {"data": {"amount": {"$gte": 10}}}})
    same(filtered["filter"], {"data": {"amount": {"$gte": 10}}})
    same(summary(hub, tid)["p"], {"id": "p", "type": "aws_sqs", "topics": ["user.updated", "user.deleted"],
                                  "filter": {"data": {"amount": {"$gte": 10}}}, "disabled": False})
    same(json.loads(dest_hash(hub, tid, "p")["filter"]), {"data": {"amount": {"$gte": 10}}})
    unchanged = hub.ok("PATCH", path, {})
    model(unchanged, {k: v for k, v in filtered.items() if k not in TIMES}, times=TIMES)
    cleared = hub.ok("PATCH", path, {"filter": {}})
    assert cleared.get("filter") is None
    assert "filter" not in dest_hash(hub, tid, "p")
    assert "filter" not in summary(hub, tid)["p"]
    same(json.loads(dest_hash(hub, tid, "p")["config"]), {"endpoint": ENDPOINT, "queue_url": url2})

    error(hub.request("PATCH", path, {"type": "aws_eventbridge"}), 422, "type cannot be updated")
    error(hub.request("PATCH", path, {"topics": []}), 422, "validation failed: invalid topics")
    error(hub.request("PATCH", path, {"topics": ["nope"]}), 422, "validation failed: invalid topics")
    error(hub.request("PATCH", path, {"config": {"queue_url": ""}}), 422, "validation error", ["config.queue_url is required"])
    error(hub.request("PATCH", path, {"metadata": "text"}), 422, "invalid JSON")
    error(hub.request("PATCH", path, raw=b""), 422, "invalid JSON")
    error(hub.request("PATCH", path, {"disabled_at": "2999-01-01T00:00:00Z"}), 422, "disabled_at cannot be in the future")
    error(hub.request("PATCH", f"/tenants/{tid}/destinations/missing", {}), 404, "destination not found")
    model(hub.ok("GET", path), {k: v for k, v in cleared.items() if k not in TIMES}, times=TIMES)
    same(hub.ok("PATCH", path, {"type": "aws_sqs"})["type"], "aws_sqs")


def test_disable_enable_and_delete(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, id="keep", topics=["user.created"])
    dest = hub.sqs_destination(tid, url, id="toggle", topics=["user.deleted"])
    path = f"/tenants/{tid}/destinations/toggle"
    start = time.time()

    disabled = hub.ok("PUT", path + "/disable")
    recent(disabled.get("disabled_at"), start)
    assert millis(disabled["updated_at"]) == millis(dest["updated_at"])
    record = dest_hash(hub, tid, "toggle")
    assert stored_number(record["disabled_at"]) == millis(disabled.get("disabled_at"))
    assert summary(hub, tid)["toggle"]["disabled"] is True
    again = hub.ok("PUT", path + "/disable")
    assert millis(again.get("disabled_at")) == millis(disabled.get("disabled_at"))

    enabled = hub.ok("PUT", path + "/enable")
    assert enabled.get("disabled_at") is None
    assert "disabled_at" not in dest_hash(hub, tid, "toggle")
    assert summary(hub, tid)["toggle"]["disabled"] is False
    assert hub.ok("PUT", path + "/enable").get("disabled_at") is None
    error(hub.request("PUT", f"/tenants/{tid}/destinations/missing/disable"), 404, "destination not found")

    past = hub.ok("PATCH", path, {"disabled_at": "2020-01-01T00:00:00Z"})
    assert millis(past.get("disabled_at")) == millis("2020-01-01T00:00:00Z")
    assert stored_number(dest_hash(hub, tid, "toggle")["disabled_at"]) == 1577836800000
    assert hub.ok("PATCH", path, {"disabled_at": None}).get("disabled_at") is None

    model(hub.ok("GET", f"/tenants/{tid}"), {"id": tid, "destinations_count": 2, "topics": ["user.created", "user.deleted"]},
          times=("created_at", "updated_at"))
    same(hub.ok("DELETE", path), {"success": True})
    error(hub.request("DELETE", path), 404, "destination not found")
    error(hub.request("GET", path), 404, "destination not found")
    assert [d["id"] for d in hub.ok("GET", f"/tenants/{tid}/destinations")] == ["keep"]
    assert list(summary(hub, tid)) == ["keep"]
    record = dest_hash(hub, tid, "toggle")
    assert abs(float(stored_number(record["deleted_at"])) / 1000 - time.time()) < 120
    assert hub.engines.valkey().ttl(hub.key(f"tenant:{{{tid}}}:destination:toggle")) > 7 * 24 * 3600 - 60
    model(hub.ok("GET", f"/tenants/{tid}"), {"id": tid, "destinations_count": 1, "topics": ["user.created"]},
          times=("created_at", "updated_at"))


def test_create_disabled_destination(hub, queues):
    tid = hub.tenant()
    dest = hub.sqs_destination(tid, queues.create(), id="off", disabled_at="2020-01-01T00:00:00Z")
    assert millis(dest.get("disabled_at")) == millis("2020-01-01T00:00:00Z")
    assert summary(hub, tid)["off"]["disabled"] is True


def test_wildcard_topic_subscriptions(make_hub, queues):
    strict = make_hub(TOPICS=None)
    tid = strict.tenant()
    error(strict.request("POST", f"/tenants/{tid}/destinations", {
        "type": "aws_sqs", "topics": ["user.*"], "config": {"queue_url": queues.create()}, "credentials": dict(CREDS)}),
        422, "validation failed: invalid topics")
    same(strict.ok("GET", "/topics"), [])
    free = strict.sqs_destination(tid, queues.create(), topics=["any.topic"])
    same(free["topics"], ["any.topic"])

    loose = make_hub(TOPICS=None, TOPICS_ALLOW_WILDCARDS="true")
    tid = loose.tenant()
    wild = loose.sqs_destination(tid, queues.create(), topics=["user.*", "order.*.paid"], id="wild")
    same(wild["topics"], ["user.*", "order.*.paid"])
    model(loose.ok("GET", f"/tenants/{tid}"), {"id": tid, "destinations_count": 1, "topics": ["order.*.paid", "user.*"]},
          times=("created_at", "updated_at"))


def test_topics_and_destination_types(hub):
    same(hub.ok("GET", "/topics"), ["user.created", "user.updated", "user.deleted"])

    def fields(spec, group):
        return [(f["key"], f["required"], bool(f.get("sensitive"))) for f in spec[group]]

    sqs = hub.ok("GET", "/destination-types/aws_sqs")
    assert sqs["type"] == "aws_sqs" and sqs["label"] == "AWS SQS"
    assert fields(sqs, "config_fields") == [("queue_url", True, False)]
    assert fields(sqs, "credential_fields") == [("key", True, True), ("secret", True, True)]
    eb = hub.ok("GET", "/destination-types/aws_eventbridge")
    assert eb["type"] == "aws_eventbridge"
    assert fields(eb, "config_fields") == [("event_bus_name", False, False), ("region", True, False), ("endpoint", False, False)]
    assert fields(eb, "credential_fields") == [("key", True, True), ("secret", True, True), ("session", False, True)]
    listed = {spec["type"]: spec for spec in hub.ok("GET", "/destination-types")}
    assert {"aws_sqs", "aws_eventbridge"} <= set(listed)
    same_model(listed["aws_sqs"], sqs)
    error(hub.request("GET", "/destination-types/nope"), 404, "destination type not found")
