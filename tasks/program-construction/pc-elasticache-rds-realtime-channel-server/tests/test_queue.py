"""The SQS webhook queue: job messages, consumption by any server, and failure handling."""

import json
import time

from realtime import ALL_EVENTS, hmac_hex, same, same_multiset, wait_for

PRODUCER_ONLY = {"queue.sqs": {"concurrency": 0}}


def receive_all(d, visibility=30):
    """Every message currently on the queue (left in flight so the server cannot take them meanwhile)."""
    out, end = [], time.monotonic() + 10
    while time.monotonic() < end:
        got = d.sqs.receive_message(QueueUrl=d.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
                                    VisibilityTimeout=visibility, AttributeNames=["All"]).get("Messages", [])
        if not got:
            break
        out.extend(got)
    return out


def check_job(body, app, events):
    job = json.loads(body)
    assert isinstance(job, dict), job
    for field, value in (("app_id", app.id), ("app_key", app.key), ("app_secret", app.secret)):
        assert job.get(field) == value, job
    payload = job.get("payload")
    assert isinstance(payload, dict) and set(payload) == {"time_ms", "events"}, job
    assert type(payload["time_ms"]) in (int, float) and abs(payload["time_ms"] - time.time() * 1000) < 120_000, job
    assert same_multiset(payload["events"], events), payload


def test_webhook_events_are_enqueued_as_jobs(deploy):
    d = deploy()
    s = d.start(sections={**PRODUCER_ONLY, "webhooks.batching": {"enabled": False}})
    app = d.app("jobs", webhooks=[{"url": d.sink.url(), "event_types": ALL_EVENTS}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("q-1")
    c.join("presence-q", app, "uq", {"a": 1})
    assert wait_for(lambda: d.queue_counts() == (3, 0), 10), d.queue_counts()
    time.sleep(1)
    messages = receive_all(d)
    assert len(messages) == 3, messages
    expected = [{"name": "channel_occupied", "channel": "q-1"}, {"name": "channel_occupied", "channel": "presence-q"},
                {"name": "member_added", "channel": "presence-q", "user_id": "uq"}]
    bodies = [json.loads(m["Body"]) for m in messages]
    for m in messages:
        check_job(m["Body"], app, [e for e in expected if e in json.loads(m["Body"])["payload"]["events"]])
    assert same_multiset([e for b in bodies for e in b["payload"]["events"]], expected), bodies
    # Nothing was delivered: this server only produces.
    assert d.sink.snapshot() == []
    c.close()


def test_batched_events_share_one_job(deploy):
    d = deploy()
    s = d.start(sections={**PRODUCER_ONLY, "webhooks.batching": {"enabled": True, "duration": 1000, "size": 100}})
    app = d.app("batchjob", webhooks=[{"url": d.sink.url(), "event_types": ["channel_occupied"]}])
    s.wait_healthy()
    c = s.connect(app)
    for i in range(3):
        c.subscribe(f"b-{i}")
    assert wait_for(lambda: sum(d.queue_counts()) >= 1, 10)
    time.sleep(2)
    messages = receive_all(d)
    events = [e for m in messages for e in json.loads(m["Body"])["payload"]["events"]]
    assert same_multiset(events, [{"name": "channel_occupied", "channel": f"b-{i}"} for i in range(3)]), events
    assert 1 <= len(messages) <= 2, messages
    for m in messages:
        check_job(m["Body"], app, json.loads(m["Body"])["payload"]["events"])
    c.close()


def test_no_job_without_a_matching_webhook(deploy):
    d = deploy()
    s = d.start(sections=PRODUCER_ONLY)
    none = d.app("none")
    members = d.app("members", webhooks=[{"url": d.sink.url(), "event_types": ["member_added"]}])
    s.wait_healthy()
    for app in (none, members):
        c = s.connect(app)
        c.join("pub-1")
        c.join("private-1", app)
        c.send({"event": "client-x", "channel": "private-1", "data": {}})
        c.close()
    time.sleep(5)
    assert d.queue_counts() == (0, 0)
    c = s.connect(members)
    c.join("presence-1", members, "z")
    assert wait_for(lambda: d.queue_counts() == (1, 0), 10), d.queue_counts()
    (message,) = receive_all(d)
    check_job(message["Body"], members, [{"name": "member_added", "channel": "presence-1", "user_id": "z"}])
    c.close()


def test_queued_jobs_are_delivered_by_a_later_server(deploy):
    d = deploy()
    producer = d.start(sections={**PRODUCER_ONLY, "webhooks.batching": {"enabled": False}})
    app = d.app("later", webhooks=[{"url": d.sink.url(), "event_types": ALL_EVENTS}])
    producer.wait_healthy()
    c = producer.connect(app)
    c.join("q-1")
    c.join("presence-q", app, "uq")
    c.close()
    assert wait_for(lambda: d.queue_counts() == (6, 0), 15), d.queue_counts()
    time.sleep(1)
    producer.stop()
    assert d.sink.snapshot() == []
    consumer = d.start()
    consumer.wait_healthy()
    assert wait_for(lambda: d.queue_counts() == (0, 0), 15), d.queue_counts()
    assert wait_for(lambda: len(d.sink.events()) >= 6, 10)
    time.sleep(1)
    for r in d.sink.snapshot():
        assert r["headers"].get("x-pusher-signature") == hmac_hex(app.secret, r["body"])
    assert same_multiset(d.sink.events(), [
        {"name": "channel_occupied", "channel": "q-1"}, {"name": "channel_occupied", "channel": "presence-q"},
        {"name": "member_added", "channel": "presence-q", "user_id": "uq"}, {"name": "channel_vacated", "channel": "q-1"},
        {"name": "channel_vacated", "channel": "presence-q"}, {"name": "member_removed", "channel": "presence-q", "user_id": "uq"}]), d.sink.events()


def test_consumer_delivers_jobs_from_other_producers(deploy):
    d = deploy()
    s = d.start()
    app = d.app("inject", webhooks=[{"url": d.sink.url("/a"), "event_types": ["channel_occupied", "member_added"]},
                                    {"url": d.sink.url("/b"), "event_types": ["member_added"]}])
    s.wait_healthy()
    # A job in the observed message format, carrying its own key and secret.
    job = {"app_key": "forged-key", "app_id": app.id, "app_secret": "forged-secret", "original_signature": "x",
           "payload": {"time_ms": 123, "events": [{"name": "channel_occupied", "channel": "inj"},
                                                   {"name": "member_added", "channel": "presence-inj", "user_id": "w"},
                                                   {"name": "custom_event", "channel": "inj"}]}}
    d.sqs.send_message(QueueUrl=d.queue_url, MessageBody=json.dumps(job))
    assert wait_for(lambda: len(d.sink.snapshot()) >= 2, 10)
    time.sleep(1.5)
    requests = {r["path"]: r for r in d.sink.snapshot()}
    assert len(d.sink.snapshot()) == 2 and set(requests) == {"/a", "/b"}, d.sink.snapshot()
    for path, events in (("/a", job["payload"]["events"][:2]), ("/b", job["payload"]["events"][1:2])):
        r = requests[path]
        assert r["headers"].get("x-pusher-key") == "forged-key"
        assert r["headers"].get("x-pusher-signature") == hmac_hex("forged-secret", r["body"])
        assert same(json.loads(r["body"]), {"time_ms": 123, "events": events}), r
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10)


def test_undecodable_messages_are_discarded(deploy):
    d = deploy()
    s = d.start()
    app = d.app("bad", webhooks=[{"url": d.sink.url(), "event_types": ALL_EVENTS}])
    s.wait_healthy()
    d.sqs.send_message(QueueUrl=d.queue_url, MessageBody="not json")
    d.sqs.send_message(QueueUrl=d.queue_url, MessageBody=json.dumps({"hello": "world"}))
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10), d.queue_counts()
    time.sleep(2)
    assert d.queue_counts() == (0, 0) and d.sink.snapshot() == []
    # The worker keeps going afterwards.
    c = s.connect(app)
    c.join("after")
    assert wait_for(lambda: d.sink.events() == [{"name": "channel_occupied", "channel": "after"}], 10), d.sink.events()
    c.close()


def test_job_for_unknown_app_is_released_for_redelivery(deploy):
    d = deploy()
    s = d.start()
    app = d.app("known")
    s.wait_healthy()
    job = {"app_key": "k", "app_id": "ghost-app", "app_secret": "s", "original_signature": "x",
           "payload": {"time_ms": 5, "events": [{"name": "channel_occupied", "channel": "g"}]}}
    d.sqs.send_message(QueueUrl=d.queue_url, MessageBody=json.dumps(job))
    time.sleep(4)
    # It is not deleted: the server keeps taking it back.
    assert sum(d.queue_counts()) == 1, d.queue_counts()
    s.stop()
    time.sleep(1)
    messages = receive_all(d, visibility=5)
    assert len(messages) == 1 and same(json.loads(messages[0]["Body"]), job), messages
    assert int(messages[0]["Attributes"]["ApproximateReceiveCount"]) >= 3, messages[0]["Attributes"]
    assert d.sink.snapshot() == []
    assert app


def test_fifo_queue(deploy):
    d = deploy(fifo=True)
    s = d.start(sections={"queue.sqs": {"fifo": True, "concurrency": 0}})
    app = d.app("fifo", webhooks=[{"url": d.sink.url(), "event_types": ALL_EVENTS}])
    s.wait_healthy()
    c = s.connect(app)
    c.join("f-1")
    assert wait_for(lambda: d.queue_counts() == (1, 0), 10), d.queue_counts()
    (message,) = receive_all(d)
    check_job(message["Body"], app, [{"name": "channel_occupied", "channel": "f-1"}])
    d.sqs.change_message_visibility(QueueUrl=d.queue_url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
    c.close()
    time.sleep(4.5)
    s.stop()
    consumer = d.start(sections={"queue.sqs": {"fifo": True}})
    consumer.wait_healthy()
    assert wait_for(lambda: len(d.sink.events()) >= 2, 15), d.sink.events()
    assert same_multiset(d.sink.events(), [{"name": "channel_occupied", "channel": "f-1"}, {"name": "channel_vacated", "channel": "f-1"}])
    assert wait_for(lambda: d.queue_counts() == (0, 0), 10)
