"""End-to-end acknowledgement: what reaches OpenSearch and which messages survive sink failures.

A fault-injecting HTTP intermediary sits between the program and OpenSearch; it can fail
whole bulk requests or individual items and records every bulk request it sees.
"""

import time

import pytest
from pipeline import FaultProxy, by_id, http, strip_time


@pytest.fixture
def proxy_factory(pipe):
    made = []

    def make(plan):
        proxy = FaultProxy(pipe.os, plan)
        made.append(proxy)
        return proxy

    yield make
    for proxy in made:
        proxy.close()


def first_only(result):
    return lambda number, _items: result if number == 1 else None


def stamps(items):
    return [src.get("timestamp") for _op, _meta, src in items]


def test_server_error_without_retries_redelivers_message(pipe, proxy_factory):
    proxy = proxy_factory(first_only(503))
    pipe.put("e.json", b'{"id":"e1"}\n{"id":"e2"}\n')
    pipe.notify("e.json")
    pipe.start(pipe.config(codec="json", sink="id_key = \"id\"\nrequest.retry_attempts = 0\nhealthcheck.enabled = false", visibility=3, endpoint=proxy.url))
    found = pipe.wait_settled(2, timeout=45)
    assert sorted(by_id(found)) == ["e1", "e2"]
    assert all(h["_version"] == 1 for h in found)
    ids = proxy.ids()
    assert len(ids) == 2 and ids == [["e1", "e2"], ["e1", "e2"]]
    # The second attempt comes from re-reading the object after redelivery.
    assert set(stamps(proxy.requests[0])).isdisjoint(stamps(proxy.requests[1]))
    assert pipe.stop() == 0


def test_server_error_is_retried_inside_the_sink(pipe, proxy_factory):
    proxy = proxy_factory(first_only(503))
    pipe.put("e.json", b'{"id":"e1"}\n{"id":"e2"}\n')
    pipe.notify("e.json")
    pipe.start(pipe.config(codec="json", sink="id_key = \"id\"\nrequest.retry_initial_backoff_secs = 1\nhealthcheck.enabled = false", visibility=30, endpoint=proxy.url))
    found = pipe.wait_settled(2)
    assert sorted(by_id(found)) == ["e1", "e2"]
    assert proxy.ids() == [["e1", "e2"], ["e1", "e2"]]
    # The same events are re-sent: no redelivery happened.
    assert stamps(proxy.requests[0]) == stamps(proxy.requests[1])
    assert proxy.requests[0] == proxy.requests[1]
    assert pipe.stop() == 0


def test_too_many_requests_is_retried(pipe, proxy_factory):
    proxy = proxy_factory(first_only(429))
    pipe.put("e.json", b'{"id":"e1"}\n')
    pipe.notify("e.json")
    pipe.start(pipe.config(codec="json", sink="id_key = \"id\"\nrequest.retry_initial_backoff_secs = 1\nhealthcheck.enabled = false", endpoint=proxy.url))
    found = pipe.wait_settled(1)
    assert sorted(by_id(found)) == ["e1"]
    assert proxy.requests[0] == proxy.requests[1]
    assert pipe.stop() == 0


def test_client_error_rejects_and_deletes(pipe, proxy_factory):
    proxy = proxy_factory(first_only(400))
    pipe.put("e.json", b'{"id":"e1"}\n')
    pipe.notify("e.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nhealthcheck.enabled = false', visibility=3, endpoint=proxy.url))
    pipe.wait_for(lambda: proxy.requests and pipe.queued() == 0, message="message deleted")
    time.sleep(8)
    assert pipe.hits() == []
    assert proxy.ids() == [["e1"]]
    assert pipe.queued() == 0
    assert pipe.stop() == 0


def test_client_error_redelivered_when_failed_messages_kept(pipe, proxy_factory):
    proxy = proxy_factory(first_only(400))
    pipe.put("e.json", b'{"id":"e1"}\n')
    pipe.notify("e.json")
    config = pipe.config(codec="json", sink='id_key = "id"\nhealthcheck.enabled = false', visibility=3, endpoint=proxy.url, sqs="delete_failed_message = false")
    pipe.start(config)
    (hit,) = pipe.wait_settled(1, timeout=45)
    assert hit["_id"] == "e1"
    assert proxy.ids() == [["e1"], ["e1"]]
    assert pipe.stop() == 0


def test_partial_retry_resends_only_failed_items(pipe, proxy_factory):
    proxy = proxy_factory(first_only({"p2": 429}))
    pipe.put("p.json", b'{"id":"p1","n":1}\n{"id":"p2","n":2}\n{"id":"p3","n":3}\n')
    pipe.notify("p.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nrequest_retry_partial = true\nrequest.retry_initial_backoff_secs = 1', endpoint=proxy.url))
    found = pipe.wait_settled(3)
    assert sorted(by_id(found)) == ["p1", "p2", "p3"]
    assert proxy.ids() == [["p1", "p2", "p3"], ["p2"]]
    assert proxy.requests[1][0] == proxy.requests[0][1]
    assert all(h["_version"] == 1 for h in found)
    assert pipe.stop() == 0


def test_partial_retry_ignores_permanent_item_errors(pipe, proxy_factory):
    proxy = proxy_factory(first_only({"p1": 400, "p2": 429}))
    pipe.put("p.json", b'{"id":"p1"}\n{"id":"p2"}\n{"id":"p3"}\n')
    pipe.notify("p.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nrequest_retry_partial = true\nrequest.retry_initial_backoff_secs = 1', endpoint=proxy.url))
    found = pipe.wait_settled(2, settle=6)
    # p1 failed permanently; once the retried p2 succeeds the message counts as delivered.
    assert sorted(by_id(found)) == ["p2", "p3"]
    assert proxy.ids() == [["p1", "p2", "p3"], ["p2"]]
    assert pipe.stop() == 0


def test_partial_failure_without_partial_retry_rejects_batch(pipe, proxy_factory):
    proxy = proxy_factory(first_only({"p2": 429}))
    pipe.put("p.json", b'{"id":"p1"}\n{"id":"p2"}\n{"id":"p3"}\n')
    pipe.notify("p.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nrequest.retry_initial_backoff_secs = 1', endpoint=proxy.url))
    found = pipe.wait_settled(2, settle=6)
    assert sorted(by_id(found)) == ["p1", "p3"]
    assert proxy.ids() == [["p1", "p2", "p3"]]
    assert pipe.stop() == 0


def test_partial_failure_redelivers_when_failed_messages_kept(pipe, proxy_factory):
    proxy = proxy_factory(first_only({"p2": 429}))
    pipe.put("p.json", b'{"id":"p1"}\n{"id":"p2"}\n{"id":"p3"}\n')
    pipe.notify("p.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', visibility=3, endpoint=proxy.url, sqs="delete_failed_message = false"))
    found = pipe.wait_settled(3, timeout=45)
    docs = by_id(found)
    assert sorted(docs) == ["p1", "p2", "p3"]
    assert [docs[k]["_version"] for k in ("p1", "p2", "p3")] == [2, 1, 2]
    assert proxy.ids() == [["p1", "p2", "p3"], ["p1", "p2", "p3"]]
    assert pipe.stop() == 0


def test_disabled_acknowledgements_delete_after_reading(pipe):
    index = pipe.prefix + "-typed"
    assert http("PUT", f"{pipe.os}/{index}", {"mappings": {"properties": {"n": {"type": "integer"}}}})[0] == 200
    pipe.put("mc.json", b'{"id":"p1","n":1}\n{"id":"p2","n":"abc"}\n')
    pipe.notify("mc.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"', index=index, visibility=3, acks=False, sqs="delete_failed_message = false"))
    found = pipe.wait_settled(1, pattern=index, settle=6)
    assert sorted(by_id(found)) == ["p1"]
    assert found[0]["_version"] == 1
    assert pipe.stop() == 0


def test_disabled_acknowledgements_lose_events_when_sink_is_down(pipe):
    pipe.put("e.json", b'{"id":"e1"}\n')
    pipe.notify("e.json")
    pipe.start(pipe.config(codec="json", sink='id_key = "id"\nrequest.retry_attempts = 0\nhealthcheck.enabled = false', visibility=3, acks=False, endpoint="http://127.0.0.1:9"))
    pipe.wait_for(lambda: pipe.queued() == 0, message="message deleted")
    time.sleep(6)
    assert pipe.queued() == 0
    assert pipe.stop() == 0


def test_unacknowledged_message_survives_restart(pipe, proxy_factory):
    down = proxy_factory(lambda _number, _items: 503)
    pipe.put("r.json", b'{"id":"r1","v":1}\n')
    pipe.notify("r.json", tag="restart")
    sink = 'id_key = "id"\nrequest.retry_attempts = 0\nhealthcheck.enabled = false'
    pipe.start(pipe.config(codec="json", sink=sink, visibility=3, endpoint=down.url))
    pipe.wait_for(lambda: len(down.requests) >= 2, message="repeated failing attempts")
    assert pipe.stop() == 0
    assert pipe.queued() == 1
    assert pipe.hits() == []
    healthy = proxy_factory(lambda _number, _items: None)
    pipe.start(pipe.config(codec="json", sink=sink, visibility=3, endpoint=healthy.url))
    (hit,) = pipe.wait_settled(1, timeout=45)
    assert hit["_id"] == "r1"
    assert strip_time(hit["_source"])["v"] == 1
    assert healthy.ids() == [["r1"]]
    assert pipe.stop() == 0


def test_batches_respect_max_events(pipe, proxy_factory):
    proxy = proxy_factory(lambda _number, _items: None)
    pipe.put("big.log", "".join(f"row {i}\n" for i in range(250)).encode())
    pipe.notify("big.log")
    pipe.start(pipe.config(sink="batch.max_events = 100", endpoint=proxy.url))
    found = pipe.wait_settled(250)
    assert sorted(h["_source"]["message"] for h in found) == sorted(f"row {i}" for i in range(250))
    sent = [[src["message"] for _op, _meta, src in items] for items in proxy.requests]
    assert sent == [[f"row {i}" for i in range(lo, hi)] for lo, hi in ((0, 100), (100, 200), (200, 250))]
    assert pipe.stop() == 0


def test_retries_exhausted_leave_message_queued(pipe, proxy_factory):
    proxy = proxy_factory(lambda _number, _items: 503)
    pipe.put("e.json", b'{"id":"e1"}\n')
    pipe.notify("e.json")
    sink = 'id_key = "id"\nrequest.retry_attempts = 2\nrequest.retry_initial_backoff_secs = 1\nhealthcheck.enabled = false'
    pipe.start(pipe.config(codec="json", sink=sink, visibility=40, endpoint=proxy.url))
    pipe.wait_for(lambda: len(proxy.requests) >= 3, message="three attempts")
    time.sleep(6)
    # One attempt plus two retries of the same request, then the message is left for redelivery.
    assert len(proxy.requests) == 3
    assert proxy.requests[0] == proxy.requests[1] == proxy.requests[2]
    assert proxy.ids()[0] == ["e1"]
    assert pipe.queued() == 1
    assert pipe.hits() == []
    assert pipe.stop() == 0
