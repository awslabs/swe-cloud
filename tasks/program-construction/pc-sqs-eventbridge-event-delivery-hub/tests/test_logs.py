"""Event and attempt logs: PostgreSQL rows and the query API."""

import json
import time
from datetime import datetime, timezone

import pytest

from hub import error, ids, millis, model, parse_time, receive, recent, same, same_model, uid

RFC = "expected RFC3339 (e.g. 2024-01-15T09:30:00Z or 2024-01-15T09:30:00.123Z)"


@pytest.fixture(scope="module")
def logged(hub, engines):
    """A tenant with a small, fully logged history (topics, filter, metadata, explicit time, disabled destination)."""
    import boto3

    sqs = boto3.client("sqs")
    run = uid()
    urls = [sqs.create_queue(QueueName=f"logs-{n}-{run}")["QueueUrl"] for n in ("a", "b")]
    try:
        tid = hub.tenant()
        other = hub.tenant()
        hub.sqs_destination(tid, urls[0], topics=["user.created"], id="dest-a")
        hub.sqs_destination(tid, urls[1], topics="*", id="dest-b", filter={"data": {"amount": {"$gte": 100}}})
        hub.sqs_destination(other, urls[0], topics="*", id="dest-a")
        published = []
        for body in (
            {"id": f"ev1-{run}", "topic": "user.created", "time": "2026-01-02T03:04:05Z", "metadata": {"source": "probe"},
             "data": {"amount": 5, "nested": {"k": [1, 2.5]}}},
            {"id": f"ev2-{run}", "topic": "user.updated", "data": {"amount": 100}},
            {"id": f"ev3-{run}", "topic": "user.created", "data": {"amount": 150}, "eligible_for_retry": False},
            {"id": f"ev4-{run}", "topic": "user.deleted", "data": {"amount": 1}},
            {"id": f"ev5-{run}", "topic": "user.updated", "data": {"amount": 7}},
        ):
            published.append(hub.publish({"tenant_id": tid, **body}))
            time.sleep(0.02)
        hub.publish({"id": f"other-{run}", "tenant_id": other, "topic": "user.updated", "data": {"amount": 1}})
        receive(urls[0], 3)
        receive(urls[1], 2)
        hub.wait_attempts(tid, 4)
        hub.wait_events(tid, 3)
        hub.wait_attempts(other, 1)
        yield {"tenant": tid, "other": other, "run": run, "published": published}
    finally:
        for url in urls:
            sqs.delete_queue(QueueUrl=url)


def test_unmatched_events_are_not_logged(hub, logged):
    run, tid = logged["run"], logged["tenant"]
    outcomes = {p["id"]: ids(p["destination_ids"]) for p in logged["published"]}
    assert outcomes == {f"ev1-{run}": ["dest-a"], f"ev2-{run}": ["dest-b"], f"ev3-{run}": ["dest-a", "dest-b"],
                        f"ev4-{run}": [], f"ev5-{run}": []}
    events = hub.ok("GET", f"/events?tenant_id={tid}&dir=asc")["models"]
    assert [e["id"] for e in events] == [f"ev1-{run}", f"ev2-{run}", f"ev3-{run}"]
    error(hub.request("GET", f"/events/ev4-{run}"), 404, "event not found")


def test_event_rows_and_models(hub, logged):
    run, tid = logged["run"], logged["tenant"]
    with hub.pg() as conn:
        rows = conn.execute("select id, tenant_id, time, topic, eligible_for_retry, data, metadata, matched_destination_ids "
                            "from events where tenant_id=%s order by id", (tid,)).fetchall()
    assert [r[0] for r in rows] == [f"ev1-{run}", f"ev2-{run}", f"ev3-{run}"]
    first, second, third = rows
    assert first[1:5] == (tid, datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "user.created", True)
    assert first[5] == '{"amount":5,"nested":{"k":[1,2.5]}}'
    same(first[6], {"source": "probe"})
    assert first[7] == ["dest-a"]
    assert (second[3], second[4], second[5], second[7]) == ("user.updated", True, '{"amount":100}', ["dest-b"])
    same(second[6], {})
    assert (third[4], third[5], sorted(third[7])) == (False, '{"amount":150}', ["dest-a", "dest-b"])

    single = hub.ok("GET", f"/events/ev1-{run}")
    model(single, {"id": f"ev1-{run}", "tenant_id": tid, "matched_destination_ids": ["dest-a"], "topic": "user.created",
                   "time": "x", "eligible_for_retry": True, "metadata": {"source": "probe"},
                   "data": {"amount": 5, "nested": {"k": [1, 2.5]}}}, times=("time",))
    assert parse_time(single["time"]) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    listed = {e["id"]: e for e in hub.ok("GET", f"/events?tenant_id={tid}")["models"]}
    same_model(listed[f"ev1-{run}"], single)
    third_model = listed[f"ev3-{run}"]
    assert ids(third_model.pop("matched_destination_ids")) == ["dest-a", "dest-b"]
    model(third_model, {"id": f"ev3-{run}", "tenant_id": tid, "topic": "user.created", "eligible_for_retry": False,
                        "data": {"amount": 150}}, times=("time",))
    assert millis(third_model["time"]) == millis(third[2].isoformat())
    same_model(hub.ok("GET", f"/events/ev1-{run}?tenant_id={tid}"), single)


def test_attempt_rows_and_models(hub, logged):
    run, tid = logged["run"], logged["tenant"]
    with hub.pg() as conn:
        rows = conn.execute(
            "select event_id, destination_id, status, code, response_data, manual, attempt_number, tenant_id, topic, event_time, "
            "eligible_for_retry, event_data, event_metadata, destination_type, latency_ms, id, time from attempts "
            "where tenant_id=%s order by event_id, destination_id", (tid,)).fetchall()
    summary = [r[:4] + r[5:9] + r[10:12] + (r[13],) for r in rows]
    assert summary == [
        (f"ev1-{run}", "dest-a", "success", "OK", False, 1, tid, "user.created", True, '{"amount":5,"nested":{"k":[1,2.5]}}', "aws_sqs"),
        (f"ev2-{run}", "dest-b", "success", "OK", False, 1, tid, "user.updated", True, '{"amount":100}', "aws_sqs"),
        (f"ev3-{run}", "dest-a", "success", "OK", False, 1, tid, "user.created", False, '{"amount":150}', "aws_sqs"),
        (f"ev3-{run}", "dest-b", "success", "OK", False, 1, tid, "user.created", False, '{"amount":150}', "aws_sqs"),
    ]
    for r in rows:
        same(json.loads(r[4]), {})
        assert isinstance(r[14], int) and 0 <= r[14] < 5000
    assert rows[0][9] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    same(rows[0][12], {"source": "probe"})
    same(rows[1][12], {})

    attempts = hub.ok("GET", f"/attempts?tenant_id={tid}&dir=asc")["models"]
    assert len(attempts) == 4
    by_key = {(a["event_id"], a["destination_id"]): a for a in attempts}
    for r in rows:
        a = by_key[(r[0], r[1])]
        model(a, {"id": r[15], "tenant_id": tid, "status": "success", "code": "OK", "attempt_number": 1, "manual": False,
                  "destination_type": "aws_sqs", "latency_ms": r[14], "event_id": r[0], "destination_id": r[1]}, times=("time",))
        assert millis(a["time"]) == millis(r[16].isoformat())
        same_model(hub.ok("GET", f"/attempts/{a['id']}"), a)
        same_model(hub.ok("GET", f"/tenants/{tid}/destinations/{r[1]}/attempts/{a['id']}"), a)
    assert [a["time"] for a in attempts] == sorted((a["time"] for a in attempts), key=parse_time)
    a_id = by_key[(f"ev1-{run}", "dest-a")]["id"]
    error(hub.request("GET", f"/tenants/{tid}/destinations/dest-b/attempts/{a_id}"), 404, "attempt not found")
    error(hub.request("GET", "/attempts/absent"), 404, "attempt not found")


def test_attempt_includes(hub, logged):
    run, tid = logged["run"], logged["tenant"]
    [plain] = hub.ok("GET", f"/attempts?tenant_id={tid}&event_id=ev1-{run}")["models"]
    [with_event] = hub.ok("GET", f"/attempts?tenant_id={tid}&event_id=ev1-{run}&include=event")["models"]
    event_view = with_event.pop("event")
    same_model(with_event, plain)
    model(event_view, {"id": f"ev1-{run}", "tenant_id": tid, "topic": "user.created", "eligible_for_retry": True,
                       "metadata": {"source": "probe"}}, times=("time",))
    assert parse_time(event_view["time"]) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    [full] = hub.ok("GET", f"/attempts?tenant_id={tid}&event_id=ev1-{run}&include=event.data&include=destination")["models"]
    same(full["event"]["data"], {"amount": 5, "nested": {"k": [1, 2.5]}})
    destination = hub.ok("GET", f"/tenants/{tid}/destinations/dest-a")
    destination.pop("credentials")
    model(full["destination"], destination, times=("created_at", "updated_at", "disabled_at"))
    assert millis(full["destination"]["created_at"]) == millis(destination["created_at"])
    single = hub.ok("GET", f"/attempts/{plain['id']}?include=event.data")
    same(single["event"]["data"], {"amount": 5, "nested": {"k": [1, 2.5]}})


def test_query_filters(hub, logged):
    run, tid, other = logged["run"], logged["tenant"], logged["other"]

    def events(query):
        return [e["id"] for e in hub.ok("GET", f"/events?tenant_id={tid}&dir=asc&{query}")["models"]]

    def attempts(query):
        return sorted((a["event_id"], a["destination_id"]) for a in hub.ok("GET", f"/attempts?tenant_id={tid}&{query}")["models"])

    assert events("topic=user.updated") == [f"ev2-{run}"]
    assert events("topic=user.updated&topic=user.created") == [f"ev1-{run}", f"ev2-{run}", f"ev3-{run}"]
    assert events("destination_id=dest-b") == [f"ev2-{run}", f"ev3-{run}"]
    assert events(f"id=ev2-{run}&id=ev1-{run}") == [f"ev1-{run}", f"ev2-{run}"]
    assert events("time[gte]=2026-01-01&time[lte]=2026-01-03") == [f"ev1-{run}"]
    assert events("time[gt]=2026-01-02T03:04:05Z") == [f"ev2-{run}", f"ev3-{run}"]
    assert events("time[lt]=2026-01-02T03:04:05Z") == []
    assert events("time[lte]=2026-01-02T03:04:05Z") == [f"ev1-{run}"]
    assert attempts("destination_id=dest-a") == [(f"ev1-{run}", "dest-a"), (f"ev3-{run}", "dest-a")]
    assert attempts(f"event_id=ev3-{run}") == [(f"ev3-{run}", "dest-a"), (f"ev3-{run}", "dest-b")]
    assert attempts("topic=user.updated") == [(f"ev2-{run}", "dest-b")]
    assert attempts("status=success&destination_type=aws_sqs") == attempts("")
    assert attempts("status=failed") == []
    assert attempts("destination_type=aws_eventbridge") == []
    assert sorted(a["event_id"] for a in hub.ok("GET", f"/tenants/{tid}/destinations/dest-b/attempts")["models"]) == \
        [f"ev2-{run}", f"ev3-{run}"]
    assert [e["tenant_id"] for e in hub.ok("GET", f"/events?tenant_id={other}")["models"]] == [other]
    everything = hub.ok("GET", "/events?limit=1000")["models"]
    assert {e["tenant_id"] for e in everything} >= {tid, other}
    both = hub.ok("GET", f"/events?tenant_id={tid}&tenant_id={other}")["models"]
    assert sorted(e["id"] for e in both) == sorted([f"ev1-{run}", f"ev2-{run}", f"ev3-{run}", f"other-{run}"])


def test_pagination(hub, logged):
    run, tid = logged["run"], logged["tenant"]
    page1 = hub.ok("GET", f"/events?tenant_id={tid}&limit=2")
    assert [e["id"] for e in page1["models"]] == [f"ev3-{run}", f"ev2-{run}"]
    pag = page1["pagination"]
    assert isinstance(pag.get("next"), str) and pag["next"]
    same_model(pag, {"order_by": "time", "dir": "desc", "limit": 2, "next": pag["next"], "prev": None})
    page2 = hub.ok("GET", f"/events?tenant_id={tid}&limit=2&next={pag['next']}")
    assert [e["id"] for e in page2["models"]] == [f"ev1-{run}"]
    assert page2["pagination"].get("next") is None and isinstance(page2["pagination"]["prev"], str)
    back = hub.ok("GET", f"/events?tenant_id={tid}&limit=2&prev={page2['pagination']['prev']}")
    assert [e["id"] for e in back["models"]] == [f"ev3-{run}", f"ev2-{run}"]
    assert back["pagination"].get("prev") is None

    asc = hub.ok("GET", f"/events?tenant_id={tid}&limit=2&dir=asc&order_by=time")
    assert [e["id"] for e in asc["models"]] == [f"ev1-{run}", f"ev2-{run}"]
    assert asc["pagination"]["dir"] == "asc"
    full = hub.ok("GET", f"/events?tenant_id={tid}")
    same_model(full["pagination"], {"order_by": "time", "dir": "desc", "limit": 100, "next": None, "prev": None})

    # Newest first: the two attempts of ev3 (relative order not compared), then ev2 and ev1.
    a1 = hub.ok("GET", f"/attempts?tenant_id={tid}&limit=2")
    a2 = hub.ok("GET", f"/attempts?tenant_id={tid}&limit=2&next={a1['pagination']['next']}")
    assert sorted((a["event_id"], a["destination_id"]) for a in a1["models"]) == [(f"ev3-{run}", "dest-a"), (f"ev3-{run}", "dest-b")]
    assert [a["event_id"] for a in a2["models"]] == [f"ev2-{run}", f"ev1-{run}"]
    assert a2["pagination"].get("next") is None and isinstance(a2["pagination"]["prev"], str)
    error(hub.request("GET", f"/events?tenant_id={tid}&next={a1['pagination']['next']}"), 400, "invalid cursor")


def test_query_validation(hub, logged):
    tid = logged["tenant"]
    cases = [
        ("dir=sideways", 422, "validation error", {"query.dir": "must be 'asc' or 'desc'"}),
        ("order_by=id", 422, "validation error", {"query.order_by": "must be one of: [time]"}),
        ("time[gte]=yesterday", 422, "validation error", {"query.time[gte]": f"invalid format, {RFC} or YYYY-MM-DD"}),
        ("time[lt]=2026-13-01", 422, "validation error", {"query.time[lt]": f"invalid format, {RFC} or YYYY-MM-DD"}),
        ("time[any]=2024-01-01", 400, "operator 'any' is not supported, use 'gte', 'lte', 'gt', or 'lt'", None),
        ("next=a&prev=b", 400, "cannot specify both 'next' and 'prev' cursors", None),
        ("limit=ten", 400, "invalid limit: must be an integer", None),
        ("limit=0", 400, "invalid limit: must be between 1 and 1000", None),
        ("limit=-1", 400, "invalid limit: must be between 1 and 1000", None),
        ("limit=1001", 400, "invalid limit: must be between 1 and 1000", None),
        ("next=not-a-cursor", 400, "invalid cursor", None),
        ("prev=not-a-cursor", 400, "invalid cursor", None),
    ]
    for prefix in (f"/events?tenant_id={tid}&", f"/attempts?tenant_id={tid}&", f"/tenants/{tid}/destinations/dest-a/attempts?"):
        for query, status, message, data in cases:
            try:
                error(hub.request("GET", prefix + query), status, message, data)
            except AssertionError as e:
                raise AssertionError(f"{prefix}{query}: {e}") from None
    assert len(hub.ok("GET", f"/events?tenant_id={tid}&limit=1000")["models"]) == 3


def test_tenant_token_scopes_logs(hub, logged):
    run, tid, other = logged["run"], logged["tenant"], logged["other"]
    jwt = "Bearer " + hub.ok("GET", f"/tenants/{tid}/token")["token"]
    events = hub.ok("GET", "/events", auth=jwt)["models"]
    assert sorted(e["id"] for e in events) == [f"ev1-{run}", f"ev2-{run}", f"ev3-{run}"]
    assert {a["tenant_id"] for a in hub.ok("GET", "/attempts", auth=jwt)["models"]} == {tid}
    error(hub.request("GET", f"/events?tenant_id={other}", auth=jwt), 403,
          "tenant_id query parameter does not match authenticated tenant")
    same(hub.ok("GET", f"/events/ev1-{run}", auth=jwt)["id"], f"ev1-{run}")
    error(hub.request("GET", f"/events/other-{run}", auth=jwt), 404, "event not found")
    [other_attempt] = hub.ok("GET", f"/attempts?tenant_id={other}")["models"]
    error(hub.request("GET", f"/attempts/{other_attempt['id']}", auth=jwt), 404, "attempt not found")


def test_deleted_destination_keeps_history(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*", id="gone")
    event_id = "hist-" + uid()
    hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.created", "data": {"h": 1}})
    receive(url, 1)
    [attempt] = hub.wait_attempts(tid, 1)
    hub.wait_events(tid, 1)
    hub.ok("DELETE", f"/tenants/{tid}/destinations/gone")
    [after] = hub.ok("GET", f"/attempts?tenant_id={tid}&include=destination")["models"]
    assert after.get("destination") is None
    after.pop("destination", None)
    same_model(after, attempt)
    [ev] = hub.ok("GET", f"/events?tenant_id={tid}&destination_id=gone")["models"]
    assert ev["id"] == event_id and ev["matched_destination_ids"] == ["gone"]
    error(hub.request("GET", f"/tenants/{tid}/destinations/gone/attempts"), 404, "destination not found")
    error(hub.request("POST", "/retry", {"event_id": event_id, "destination_id": "gone"}), 404, "destination not found")
