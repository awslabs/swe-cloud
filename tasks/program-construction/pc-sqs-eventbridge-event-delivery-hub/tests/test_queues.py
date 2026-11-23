"""Internal queue provisioning, the SQS publish queue, and server lifecycle."""

import json
import time

import pytest

from hub import ENDPOINT, assert_quiet, metadata_attribute, receive, same, sqs, uid


def queue_attributes(name):
    url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    return url, sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]


def depth(url):
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return int(attrs["ApproximateNumberOfMessages"]) + int(attrs["ApproximateNumberOfMessagesNotVisible"])


def test_internal_queues_are_provisioned(make_hub):
    hub = make_hub(start=False)
    before = sqs.list_queues(QueueNamePrefix=f"hub-dlv-{hub.id}").get("QueueUrls", []) + \
        sqs.list_queues(QueueNamePrefix=f"hub-log-{hub.id}").get("QueueUrls", [])
    assert before == []
    hub.start()
    for name in (hub.delivery_queue, hub.log_queue):
        url, attrs = queue_attributes(name)
        _, dlq = queue_attributes(name + "-dlq")
        policy = json.loads(attrs["RedrivePolicy"])
        assert policy["deadLetterTargetArn"] == dlq["QueueArn"]
        assert int(policy["maxReceiveCount"]) == 6
        assert attrs["VisibilityTimeout"] == "30"
        assert "RedrivePolicy" not in dlq
    # A restart reuses the existing queues.
    hub.stop()
    hub.start(migrate=False)
    names = sorted(u.rsplit("/", 1)[1] for u in sqs.list_queues(QueueNamePrefix=f"hub-").get("QueueUrls", []) if hub.id in u)
    assert names == sorted([hub.delivery_queue, hub.delivery_queue + "-dlq", hub.log_queue, hub.log_queue + "-dlq"])


def test_graceful_shutdown_keeps_state(make_hub, queues):
    hub = make_hub()
    tid = hub.tenant({"k": "v"})
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*", id="persist")
    hub.stop()
    assert hub.proc is None
    hub.start(migrate=False)
    model = hub.ok("GET", f"/tenants/{tid}")
    assert model["destinations_count"] == 1 and model["metadata"] == {"k": "v"}
    same(hub.publish({"tenant_id": tid, "topic": "user.updated", "data": {"after": "restart"}})["destination_ids"], ["persist"])
    # A receive left pending by the stopped process can hide the first message for one visibility timeout.
    assert [m["Body"] for m in receive(url, 1, timeout=45)] == ['{"after":"restart"}']
    proc = hub.proc
    proc.terminate()
    assert proc.wait(20) == 0
    hub.proc = None


def publish_queue_settings(name, **extra):
    return {"PUBLISH_AWS_SQS_QUEUE": name, "PUBLISH_AWS_SQS_REGION": "us-east-1", "PUBLISH_AWS_SQS_ENDPOINT": ENDPOINT,
            "PUBLISH_AWS_SQS_ACCESS_KEY_ID": "test", "PUBLISH_AWS_SQS_SECRET_ACCESS_KEY": "test", **extra}


def test_publish_queue_ingestion(make_hub, queues):
    pub = queues.create("pub", VisibilityTimeout=2)
    hub = make_hub(**publish_queue_settings(pub.rsplit("/", 1)[1]))
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics=["user.updated"], id="upd")
    hub.sqs_destination(tid, url, topics=["user.created"], id="cre", filter={"data": {"via": "queue"}})
    event_id = "pq-" + uid()
    sqs.send_message(QueueUrl=pub, MessageBody=json.dumps({
        "id": event_id, "tenant_id": tid, "topic": "user.updated", "data": {"via": "queue", "n": 1}, "metadata": {"m": "1"},
        "time": "2026-03-04T05:06:07Z"}))
    sqs.send_message(QueueUrl=pub, MessageBody=json.dumps({"tenant_id": tid, "topic": "user.created", "data": {"via": "queue"}}))
    sqs.send_message(QueueUrl=pub, MessageBody=json.dumps({"tenant_id": tid, "topic": "user.created", "data": {"via": "other"}}))
    messages = sorted(receive(url, 2), key=lambda m: m["Body"])
    assert [m["Body"] for m in messages] == ['{"via":"queue","n":1}', '{"via":"queue"}']
    meta = metadata_attribute(messages[0])
    assert (meta["event-id"], meta["topic"], meta["m"]) == (event_id, "user.updated", "1")
    assert_quiet(url)
    deadline = time.time() + 10
    while depth(pub) and time.time() < deadline:
        time.sleep(0.5)
    assert depth(pub) == 0
    events = {e["id"]: e for e in hub.wait_events(tid, 2)}
    assert set(events) >= {event_id}
    assert events[event_id]["time"].startswith("2026-03-04T05:06:07")
    assert len(events) == 2
    same(hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.updated", "data": {}}),
         {"id": event_id, "duplicate": True, "destination_ids": ["upd"]})


def test_publish_queue_limited_redelivery(make_hub, queues):
    pub = queues.create("pub", VisibilityTimeout=2)
    hub = make_hub(**publish_queue_settings(pub.rsplit("/", 1)[1], PUBLISH_MAX_REDELIVERIES=2))
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*", id="all")
    for body in ("not json", json.dumps({"tenant_id": tid, "topic": "nope", "data": {"x": 1}}),
                 json.dumps({"tenant_id": tid, "topic": "user.updated"}), json.dumps({"tenant_id": tid, "data": {"x": 1}}),
                 json.dumps({"tenant_id": "absent-" + uid(), "topic": "user.updated", "data": {"x": 1}})):
        sqs.send_message(QueueUrl=pub, MessageBody=body)
    sqs.send_message(QueueUrl=pub, MessageBody=json.dumps({"tenant_id": tid, "topic": "user.updated", "data": {"ok": True}}))
    assert [m["Body"] for m in receive(url, 1)] == ['{"ok":true}']
    deadline = time.time() + 25
    while depth(pub) and time.time() < deadline:
        time.sleep(0.5)
    assert depth(pub) == 0
    assert_quiet(url, wait=1)
    assert len(hub.wait_events(tid, 1)) == 1


@pytest.mark.parametrize("limit", [None, 30], ids=["unlimited", "limit-30"])
def test_publish_queue_keeps_redelivering_below_limit(make_hub, queues, limit):
    pub = queues.create("pub", VisibilityTimeout=1)
    hub = make_hub(**publish_queue_settings(pub.rsplit("/", 1)[1], PUBLISH_MAX_REDELIVERIES=limit))
    sqs.send_message(QueueUrl=pub, MessageBody="not json")
    time.sleep(8)
    assert depth(pub) == 1
    hub.stop()
    time.sleep(2)
    [message] = receive(pub, 1, timeout=5)
    assert message["Body"] == "not json"
    assert int(message["Attributes"]["ApproximateReceiveCount"]) >= 3


def test_existing_queues_without_auto_provision(make_hub, queues):
    hub = make_hub(start=False, MQS_AUTO_PROVISION="false")
    hub.migrate()
    refused = hub.cli("serve", timeout=30)
    assert refused.returncode != 0
    assert "required message queues do not exist" in refused.stderr
    assert [u for u in sqs.list_queues(QueueNamePrefix="hub-").get("QueueUrls", []) if hub.id in u] == []

    # The primary queues alone are not enough; their dead-letter queues must exist too.
    for name in (hub.delivery_queue, hub.log_queue):
        queues.urls.append(sqs.create_queue(QueueName=name)["QueueUrl"])
    refused = hub.cli("serve", timeout=30)
    assert refused.returncode != 0 and "required message queues do not exist" in refused.stderr

    for name in (hub.delivery_queue + "-dlq", hub.log_queue + "-dlq"):
        queues.urls.append(sqs.create_queue(QueueName=name)["QueueUrl"])
    hub.start(migrate=False)
    _, attrs = queue_attributes(hub.delivery_queue)
    assert "RedrivePolicy" not in attrs
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*")
    hub.publish({"tenant_id": tid, "topic": "user.created", "data": {"provisioned": False}})
    assert [m["Body"] for m in receive(url, 1)] == ['{"provisioned":false}']
    assert len(hub.wait_attempts(tid, 1)) == 1
