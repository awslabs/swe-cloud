"""Sink behaviour: document ids, index templates, bulk actions, field filters, rejections, data streams."""

import time
from datetime import UTC, datetime

from pipeline import assert_same_multiset, by_id, http, parse_instant, same, strip_time


def base(pipe, key):
    return {"bucket": pipe.bucket, "object": key, "region": "us-east-1", "source_type": "aws_s3"}


def test_id_key_sets_document_id_and_removes_field(pipe):
    lines = [
        '{"id":"j1","kind":"string"}',
        '{"id":"has space/slash","kind":"spaced"}',
        '{"id":7,"kind":"int"}',
        '{"id":7.5,"kind":"float"}',
        '{"id":true,"kind":"bool"}',
        '{"id":null,"kind":"null"}',
        '{"id":{"x":1},"kind":"object"}',
        '{"id":["l1"],"kind":"list"}',
        '{"kind":"absent"}',
    ]
    pipe.put("ids.json", ("\n".join(lines) + "\n").encode())
    pipe.notify("ids.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"'))
    found = pipe.wait_settled(len(lines))
    docs = by_id(found)
    assert same(strip_time(docs["j1"]["_source"]), {**base(pipe, "ids.json"), "kind": "string"})
    assert same(strip_time(docs["has space/slash"]["_source"]), {**base(pipe, "ids.json"), "kind": "spaced"})
    # Non-string values are removed too, but do not become the document id.
    assert_same_multiset([strip_time(h["_source"]) for h in found], [{**base(pipe, "ids.json"), "kind": k} for k in ["string", "spaced", "int", "float", "bool", "null", "object", "list", "absent"]])
    generated = [h["_id"] for h in found if h["_id"] not in ("j1", "has space/slash")]
    assert len(set(generated)) == 7
    assert not set(generated) & {"7", "7.5", "true", "null", "l1", "None", ""}
    assert pipe.stop() == 0


def test_empty_string_id_rejects_its_batch(pipe):
    pipe.put("empty-id.json", b'{"id":"ok1"}\n{"id":"","kind":"empty"}\n{"id":"ok2"}\n')
    pipe.notify("empty-id.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', visibility=3))
    found = pipe.wait_settled(2, settle=6)
    assert sorted(by_id(found)) == ["ok1", "ok2"]
    assert pipe.stop() == 0


def test_nested_id_key_and_field_template(pipe):
    pipe.put("n.json", b'{"meta":{"id":"n1","k":1},"v":1}\n{"meta":{"k":2},"v":2}\n{"id":"top","v":3}\n')
    pipe.notify("n.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "meta.id"', index=pipe.prefix + "-{{ meta.k }}"))
    found = pipe.wait_settled(2)
    one = [h for h in found if h["_index"] == f"{pipe.prefix}-1"]
    two = [h for h in found if h["_index"] == f"{pipe.prefix}-2"]
    assert len(one) == 1 and len(two) == 1
    assert one[0]["_id"] == "n1"
    assert same(strip_time(one[0]["_source"]), {**base(pipe, "n.json"), "meta": {"k": 1}, "v": 1})
    assert two[0]["_id"] != "n1"
    assert same(strip_time(two[0]["_source"]), {**base(pipe, "n.json"), "meta": {"k": 2}, "v": 2})
    # The event without meta.k cannot be routed and is dropped; the message is still deleted.
    assert pipe.stop() == 0


def test_index_template_routes_by_field_and_drops_unroutable(pipe):
    pipe.put("t.json", b'{"id":"t1","level":"info"}\n{"id":"t2","level":5}\n{"id":"t3"}\n{"id":"t4","app":{"name":"web"},"level":"debug"}\n')
    pipe.notify("t.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', index=pipe.prefix + "-{{ level }}-%Y"))
    found = pipe.wait_settled(3)
    year = datetime.now(UTC).strftime("%Y")
    assert sorted((h["_id"], h["_index"]) for h in found) == [
        ("t1", f"{pipe.prefix}-info-{year}"),
        ("t2", f"{pipe.prefix}-5-{year}"),
        ("t4", f"{pipe.prefix}-debug-{year}"),
    ]
    assert same(by_id(found)["t2"]["_source"]["level"], 5)
    assert pipe.stop() == 0


def test_template_fallback_index(pipe):
    pipe.put("t.json", b'{"id":"t1","level":"info"}\n{"id":"t3"}\n{"id":"t8","app":{"name":"web"}}\n')
    pipe.notify("t.json")
    pipe.start(pipe.config(codec="json", sink=f'id_key = "id"\nbulk.template_fallback_index = "{pipe.prefix}-unrouted"', index=pipe.prefix + "-{{ level }}"))
    found = pipe.wait_settled(3)
    assert sorted((h["_id"], h["_index"]) for h in found) == [("t1", f"{pipe.prefix}-info"), ("t3", f"{pipe.prefix}-unrouted"), ("t8", f"{pipe.prefix}-unrouted")]
    assert same(strip_time(by_id(found)["t8"]["_source"]), {**base(pipe, "t.json"), "app": {"name": "web"}})
    assert pipe.stop() == 0


def test_rejected_items_do_not_block_the_rest(pipe):
    index = pipe.prefix + "-typed"
    assert http("PUT", f"{pipe.os}/{index}", {"mappings": {"properties": {"n": {"type": "integer"}}}})[0] == 200
    pipe.put("mc.json", b'{"id":"p1","n":1}\n{"id":"p2","n":"abc"}\n{"id":"p3","n":3}\n')
    pipe.notify("mc.json", tag="mapping")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', index=index, visibility=3))
    found = pipe.wait_settled(2, settle=6)
    docs = by_id(found)
    assert sorted(docs) == ["p1", "p3"]
    assert same(strip_time(docs["p3"]["_source"]), {**base(pipe, "mc.json"), "n": 3})
    assert pipe.stop() == 0


def test_rejection_keeps_message_when_failed_messages_are_retained(pipe):
    index = pipe.prefix + "-typed"
    assert http("PUT", f"{pipe.os}/{index}", {"mappings": {"properties": {"n": {"type": "integer"}}}})[0] == 200
    pipe.put("mc.json", b'{"id":"p1","n":1}\n{"id":"p2","n":"abc"}\n')
    pipe.notify("mc.json", tag="mapping")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', index=index, visibility=3, sqs="delete_failed_message = false"))
    pipe.wait_for(lambda: pipe.count(index) == 1 and pipe.hits(index)[0]["_version"] >= 3, timeout=30, message="re-processing")
    assert sorted(by_id(pipe.hits(index))) == ["p1"]
    assert pipe.stop() == 0
    left = pipe.remaining()
    assert [tag for tag, _ in left.values()] == ["mapping"]


def run_redelivery(pipe, action, sqs="", visibility=10):
    """Index an object, overwrite it, announce it again; return the hits."""
    pipe.put("cc.json", b'{"id":"c1","v":1}\n{"id":"c2","v":1}\n')
    pipe.notify("cc.json", tag="first")
    pipe.start(pipe.config(codec="json", sink=f'id_key = "id"\nbulk.action = "{action}"', visibility=visibility, sqs=sqs))
    first = pipe.wait_settled(2)
    pipe.put("cc.json", b'{"id":"c1","v":2}\n{"id":"c2","v":2}\n{"id":"c9","v":2}\n')
    pipe.notify("cc.json", tag="second")
    return first


def test_create_action_keeps_existing_documents(pipe):
    first = by_id(run_redelivery(pipe, "create"))
    found = pipe.wait_settled(3, settle=5)
    docs = by_id(found)
    for key in ("c1", "c2"):
        assert docs[key]["_version"] == 1
        assert docs[key]["_source"] == first[key]["_source"]
        assert same(docs[key]["_source"]["v"], 1)
    assert same(strip_time(docs["c9"]["_source"]), {**base(pipe, "cc.json"), "v": 2})
    assert pipe.stop() == 0


def test_create_conflict_retained_when_failed_messages_kept(pipe):
    run_redelivery(pipe, "create", sqs="delete_failed_message = false", visibility=3)
    pipe.wait_for(lambda: pipe.count() == 3, message="c9 created")
    time.sleep(8)
    docs = by_id(pipe.hits())
    assert [docs[k]["_version"] for k in ("c1", "c2")] == [1, 1]
    assert docs["c9"]["_version"] == 1
    assert pipe.stop() == 0
    left = pipe.remaining()
    assert [tag for tag, _ in left.values()] == ["second"]
    assert all(count >= 3 for _tag, count in left.values()), left


def test_index_action_overwrites_on_redelivery(pipe):
    run_redelivery(pipe, "index")
    pipe.wait_for(lambda: pipe.count() == 3 and pipe.queued() == 0, message="second delivery indexed")
    time.sleep(2)
    docs = by_id(pipe.hits())
    for key in ("c1", "c2"):
        assert docs[key]["_version"] == 2
        assert same(strip_time(docs[key]["_source"]), {**base(pipe, "cc.json"), "v": 2})
    assert docs["c9"]["_version"] == 1
    assert pipe.queued() == 0
    assert pipe.stop() == 0


def test_action_template_per_event(pipe):
    index = pipe.prefix + "-fixed"
    for key in ("a1", "a2"):
        assert http("PUT", f"{pipe.os}/{index}/_doc/{key}?refresh=true", {"pre": "existing"})[0] in (200, 201)
    pipe.put("a.json", b'{"id":"a1","op":"create"}\n{"id":"a2","op":"index"}\n{"id":"a3"}\n{"id":"a6","op":"create"}\n')
    pipe.notify("a.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nbulk.action = "{{ op }}"', index=index))
    found = pipe.wait_settled(3, pattern=index, settle=4)
    docs = by_id(found)
    assert sorted(docs) == ["a1", "a2", "a6"]
    assert docs["a1"]["_source"] == {"pre": "existing"}
    assert same(strip_time(docs["a2"]["_source"]), {**base(pipe, "a.json"), "op": "index"})
    assert docs["a2"]["_version"] == 2
    assert same(strip_time(docs["a6"]["_source"]), {**base(pipe, "a.json"), "op": "create"})
    assert pipe.stop() == 0


def test_except_fields_removes_listed_paths(pipe):
    pipe.put("ff.json", b'{"id":"f1","keep":1,"drop":2,"nest":{"a":1,"b":2}}\n')
    pipe.notify("ff.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nencoding.except_fields = ["region", "drop", "nest.b"]'))
    (hit,) = pipe.wait_settled(1)
    source = hit["_source"]
    parse_instant(source.pop("timestamp"))
    assert same(source, {"bucket": pipe.bucket, "object": "ff.json", "source_type": "aws_s3", "keep": 1, "nest": {"a": 1}})
    assert pipe.stop() == 0


def test_only_fields_keeps_listed_paths(pipe):
    pipe.put("fo.json", b'{"id":"f1","keep":1,"drop":2,"nest":{"a":1,"b":2}}\n')
    pipe.notify("fo.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nencoding.only_fields = ["keep", "nest.a", "object"]'))
    (hit,) = pipe.wait_settled(1)
    assert hit["_id"] == "f1"
    assert same(hit["_source"], {"keep": 1, "nest": {"a": 1}, "object": "fo.json"})
    assert pipe.stop() == 0


def data_stream_template(pipe, pattern):
    name = "bei-template-" + pipe.tag
    assert http("PUT", f"{pipe.os}/_index_template/{name}", {"index_patterns": [pattern], "data_stream": {}, "priority": 200})[0] == 200
    pipe.cleanups.append(lambda: http("DELETE", f"{pipe.os}/_index_template/{name}"))
    pipe.cleanups.append(lambda: http("DELETE", f"{pipe.os}/_data_stream/{pattern}"))


def test_data_stream_mode(pipe):
    dataset = "bei" + pipe.tag
    data_stream_template(pipe, f"logs-{dataset}*")
    pipe.put("ds.json", b'{"id":"d1","msg":"x"}\n{"id":"d2","data_stream":{"namespace":"other"}}\n{"id":"d3","data_stream":{"dataset":"Bad/Name"}}\n{"id":"d4","timestamp":"2020-01-01T00:00:00Z"}\n')
    pipe.notify("ds.json")
    pipe.start(pipe.config(codec="json", bulk=False, sink=f'mode = "data_stream"\ndata_stream.dataset = "{dataset}"\ndata_stream.namespace = "ns"\nid_key = "id"'))
    pattern = f"logs-{dataset}-*"
    found = pipe.wait_settled(3, pattern=pattern)
    docs = by_id(found)
    assert sorted(docs) == ["d1", "d2", "d4"]
    assert docs["d1"]["_index"].startswith(f".ds-logs-{dataset}-ns-")
    assert docs["d2"]["_index"].startswith(f".ds-logs-{dataset}-other-")
    assert docs["d4"]["_index"].startswith(f".ds-logs-{dataset}-ns-")
    stream = {"type": "logs", "dataset": dataset, "namespace": "ns"}
    d1 = dict(docs["d1"]["_source"])
    parse_instant(d1.pop("@timestamp"))
    assert same(d1, {**base(pipe, "ds.json"), "msg": "x", "data_stream": stream})
    d2 = dict(docs["d2"]["_source"])
    parse_instant(d2.pop("@timestamp"))
    assert same(d2, {**base(pipe, "ds.json"), "data_stream": {**stream, "namespace": "other"}})
    assert same(docs["d4"]["_source"], {**base(pipe, "ds.json"), "@timestamp": "2020-01-01T00:00:00Z", "data_stream": stream})
    code, streams = http("GET", f"{pipe.os}/_data_stream/logs-{dataset}*")
    assert code == 200
    assert sorted(s["name"] for s in streams["data_streams"]) == [f"logs-{dataset}-ns", f"logs-{dataset}-other"]
    assert pipe.stop() == 0


def test_data_stream_without_field_sync(pipe):
    dataset = "beib" + pipe.tag
    data_stream_template(pipe, f"logs-{dataset}*")
    head = pipe.put("ds.log", b"plain line\n")
    pipe.notify("ds.log")
    pipe.start(pipe.config(bulk=False, sink=f'mode = "data_stream"\ndata_stream.dataset = "{dataset}"\ndata_stream.sync_fields = false'))
    (hit,) = pipe.wait_settled(1, pattern=f"logs-{dataset}-*")
    assert hit["_index"].startswith(f".ds-logs-{dataset}-default-")
    source = dict(hit["_source"])
    assert parse_instant(source.pop("@timestamp")) == head["LastModified"]
    assert same(source, {**base(pipe, "ds.log"), "message": "plain line"})
    assert pipe.stop() == 0


def test_data_stream_with_auto_routing_disabled(pipe):
    dataset = "beir" + pipe.tag
    data_stream_template(pipe, f"metrics-{dataset}*")
    pipe.put("ar.json", b'{"id":"r1","data_stream":{"namespace":"other","dataset":"elsewhere"}}\n{"id":"r2"}\n')
    pipe.notify("ar.json")
    sink = f'mode = "data_stream"\ndata_stream.type = "metrics"\ndata_stream.dataset = "{dataset}"\ndata_stream.namespace = "ns"\ndata_stream.auto_routing = false\nid_key = "id"'
    pipe.start(pipe.config(codec="json", bulk=False, sink=sink))
    found = pipe.wait_settled(2, pattern=f"metrics-{dataset}-*")
    docs = by_id(found)
    for hit in found:
        assert hit["_index"].startswith(f".ds-metrics-{dataset}-ns-")
    r1, r2 = dict(docs["r1"]["_source"]), dict(docs["r2"]["_source"])
    parse_instant(r1.pop("@timestamp"))
    parse_instant(r2.pop("@timestamp"))
    # Field sync only fills in what the event lacks; its own data_stream values stay.
    assert same(r1, {**base(pipe, "ar.json"), "data_stream": {"type": "metrics", "dataset": "elsewhere", "namespace": "other"}})
    assert same(r2, {**base(pipe, "ar.json"), "data_stream": {"type": "metrics", "dataset": dataset, "namespace": "ns"}})
    assert pipe.stop() == 0
