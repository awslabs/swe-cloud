"""SQS notification handling: accepted formats, key decoding, records, and messages that stay queued."""

import time

from pipeline import record, s3_test_event, sns_envelope, strip_time


def objects(found):
    return sorted(h["_source"]["object"] for h in found)


def test_accepted_notification_formats(pipe):
    for key in ("direct.log", "sns.log", "copy.log", "v20.log"):
        pipe.put(key, key.encode() + b"\n")
    pipe.notify("direct.log")
    pipe.send(sns_envelope({"Records": [record(pipe.bucket, "sns.log")]}))
    pipe.send(s3_test_event(pipe.bucket))
    pipe.send(sns_envelope(s3_test_event(pipe.bucket)))
    pipe.notify("direct.log", name="ObjectRemoved:Delete")
    pipe.notify("copy.log", name="ObjectCreated:Copy")
    pipe.notify("v20.log", version="2.0")
    pipe.send({"Records": []})
    pipe.start(pipe.config())
    found = pipe.wait_settled(4)
    assert objects(found) == ["copy.log", "direct.log", "sns.log", "v20.log"]
    for hit in found:
        assert hit["_source"]["message"] == hit["_source"]["object"]
    assert pipe.stop() == 0


def test_object_keys_are_url_decoded(pipe):
    keys = {
        "dir/a+b%2Bc%25d+%C3%A9.log": "dir/a b+c%d é.log",
        "literal%252F.log": "literal%2F.log",
        "plain/name-1_x.log": "plain/name-1_x.log",
        "spaces%20encoded.log": "spaces encoded.log",
    }
    for encoded, key in keys.items():
        pipe.put(key, f"content of {encoded}\n".encode())
        pipe.notify(encoded)
    pipe.start(pipe.config())
    found = pipe.wait_settled(len(keys))
    assert sorted((h["_source"]["object"], h["_source"]["message"]) for h in found) == sorted((key, f"content of {encoded}") for encoded, key in keys.items())
    assert pipe.stop() == 0


def test_records_in_one_message_are_processed_in_order(pipe):
    for key in ("a.log", "b.log", "c.log", "d.log"):
        pipe.put(key, key.encode() + b"\n")
    pipe.send({"Records": [record(pipe.bucket, "missing.log"), record(pipe.bucket, "a.log")], "probeTag": "missing-then-a"})
    pipe.send({"Records": [record(pipe.bucket, "a.log", name="ObjectRemoved:Delete"), record(pipe.bucket, "b.log")], "probeTag": "removed-then-b"})
    pipe.send({"Records": [record(pipe.bucket, "c.log"), record(pipe.bucket, "c.log", region="eu-west-1")], "probeTag": "c-then-wrong-region"})
    pipe.send({"Records": [record(pipe.bucket, "d.log"), record(pipe.bucket, "d.log")], "probeTag": "d-twice"})
    pipe.start(pipe.config(visibility=3))
    pipe.wait_for(lambda: pipe.queued() == 2 and {"b.log", "d.log"} <= set(objects(pipe.hits())), message="good messages processed")
    pipe.wait_for(lambda: objects(pipe.hits()).count("c.log") >= 2, timeout=20, message="redelivery of the c.log message")
    time.sleep(3)
    found = objects(pipe.hits())
    # A failing record stops the message: later records are not reached, earlier ones were
    # delivered. (A message still waiting in a receive batch may be received twice, so
    # successful messages are counted per delivery, not exactly once.)
    assert "a.log" not in found
    assert found.count("b.log") >= 1
    assert found.count("d.log") >= 2 and found.count("d.log") % 2 == 0
    assert found.count("c.log") >= 2
    assert pipe.stop() == 0
    left = pipe.remaining()
    assert sorted(tag for tag, _ in left.values()) == ["c-then-wrong-region", "missing-then-a"]
    assert all(count >= 3 for _tag, count in left.values()), left


def bad_record(pipe, key, **changes):
    item = record(pipe.bucket, key)
    for field, value in changes.items():
        if value is None:
            del item[field]
        else:
            item[field] = value
    return item


def test_unprocessable_messages_stay_queued_and_are_redelivered(pipe):
    for key in ("ok.log", "region.log", "v1.log", "v3.log", "v2.log", "sns.log", "name.log", "time.log"):
        pipe.put(key, key.encode() + b"\n")
    pipe.notify("ok.log", tag="ok")
    pipe.notify("region.log", region="eu-west-1", tag="wrong-region")
    pipe.notify("v1.log", version="1.0", tag="version-1.0")
    pipe.notify("v3.log", version="3.1", tag="version-3.1")
    pipe.notify("v2.log", version="2", tag="version-without-minor")
    pipe.send({"Records": [bad_record(pipe, "name.log", eventName="ObjectCreatedPut")], "probeTag": "name-without-kind"})
    pipe.send({"Records": [bad_record(pipe, "time.log", eventTime=None)], "probeTag": "no-event-time"})
    pipe.send({**sns_envelope({"Records": [record(pipe.bucket, "sns.log")]}, timestamp=False), "probeTag": "sns-without-timestamp"})
    pipe.send({"probeTag": "not-an-event", "Message": "nothing"})
    pipe.send("plain text body")
    pipe.notify("never-written.log", tag="missing-object")
    pipe.send({"Records": [record(pipe.bucket + "-absent", "ok.log")], "probeTag": "missing-bucket"})
    expected_left = sorted(
        [
            "wrong-region", "version-1.0", "version-3.1", "version-without-minor", "name-without-kind", "no-event-time",
            "sns-without-timestamp", "not-an-event", "plain text body", "missing-object", "missing-bucket",
        ]
    )
    pipe.start(pipe.config(visibility=3))
    pipe.wait_for(lambda: pipe.count() >= 1 and pipe.queued() == len(expected_left), message="ok processed")
    time.sleep(8)
    found = pipe.hits()
    assert found
    for hit in found:
        assert strip_time(hit["_source"]) == {"bucket": pipe.bucket, "object": "ok.log", "region": "us-east-1", "source_type": "aws_s3", "message": "ok.log"}
    assert pipe.stop() == 0
    left = pipe.remaining()
    assert sorted(tag for tag, _ in left.values()) == expected_left
    assert all(count >= 3 for _tag, count in left.values()), left


def test_delete_message_false_leaves_handled_messages(pipe):
    pipe.put("keep.json", b'{"id":"k1","v":1}\n')
    pipe.notify("keep.json", tag="created")
    pipe.send({**s3_test_event(pipe.bucket), "probeTag": "test-event"})
    pipe.notify("keep.json", name="ObjectRemoved:Delete", tag="removed")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', visibility=3, sqs="delete_message = false"))
    pipe.wait_for(lambda: pipe.count() == 1, message="document indexed")
    # Re-receiving re-indexes the same document under its id.
    pipe.wait_for(lambda: pipe.hits()[0]["_version"] >= 3, timeout=30, message="re-processing")
    (hit,) = pipe.hits()
    assert hit["_id"] == "k1"
    assert pipe.stop() == 0
    left = pipe.remaining()
    assert sorted(tag for tag, _ in left.values()) == ["created", "removed", "test-event"]
    assert all(count >= 3 for _tag, count in left.values()), left
