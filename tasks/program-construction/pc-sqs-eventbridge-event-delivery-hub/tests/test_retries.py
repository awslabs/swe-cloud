"""Failure handling over time: automatic retries, manual retry, and consecutive-failure auto-disable."""

import time

from hub import stored_number, assert_quiet, error, metadata_attribute, millis, parse_time, receive, same, sqs, uid


def summary(attempts):
    return [(a["event_id"], a["destination_id"], a["status"], a["code"], a["attempt_number"], a["manual"]) for a in attempts]


def gaps(attempts):
    times = [parse_time(a["time"]).timestamp() for a in attempts]
    return [round(b - a, 1) for a, b in zip(times, times[1:])]


def test_scheduled_retries_until_success(make_hub, queues):
    hub = make_hub(RETRY_SCHEDULE="1,2,2")
    tid = hub.tenant()
    base = queues.create("base").rsplit("/", 1)[0]
    name = "late-" + uid()
    url = f"{base}/{name}"
    hub.sqs_destination(tid, url, id="late")
    event_id = "r-" + uid()
    hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.created", "data": {"n": 1}})
    first = hub.wait_attempts(tid, 1, timeout=10)
    assert summary(first) == [(event_id, "late", "failed", "ERR", 1, False)]
    # The target queue appears after the first failure; a later scheduled retry delivers.
    queues.urls.append(sqs.create_queue(QueueName=name)["QueueUrl"])
    [message] = receive(url, 1, timeout=20)
    assert message["Body"] == '{"n":1}'
    assert metadata_attribute(message)["event-id"] == event_id
    attempts = hub.wait_attempts(tid, 3, timeout=10)
    failures = len(attempts) - 1
    assert 1 <= failures <= 3
    assert summary(attempts) == [(event_id, "late", "failed", "ERR", n, False) for n in range(1, failures + 1)] + \
        [(event_id, "late", "success", "OK", failures + 1, False)]
    expected_gaps = [1, 2, 2][:failures]
    assert all(want - 0.3 <= got <= want + 2 for got, want in zip(gaps(attempts), expected_gaps)), gaps(attempts)
    time.sleep(3)
    assert len(hub.attempts(tid)) == failures + 1
    assert_quiet(url, wait=1)
    assert hub.ok("GET", f"/tenants/{tid}/destinations/late").get("disabled_at") is None


def test_schedule_length_limits_retries(make_hub, queues):
    hub = make_hub(RETRY_SCHEDULE="1,2")
    tid = hub.tenant()
    hub.sqs_destination(tid, queues.missing(), topics=["user.deleted"], id="miss")
    hub.sqs_destination(tid, queues.missing(), topics=["user.updated"], id="once")
    retried, single = "r-" + uid(), "s-" + uid()
    hub.publish({"id": retried, "tenant_id": tid, "topic": "user.deleted", "data": {}})
    hub.publish({"id": single, "tenant_id": tid, "topic": "user.updated", "data": {}, "eligible_for_retry": False})
    attempts = hub.wait_attempts(tid, 4, timeout=15)
    time.sleep(4)
    attempts = hub.attempts(tid)
    miss = [a for a in attempts if a["destination_id"] == "miss"]
    assert summary(miss) == [(retried, "miss", "failed", "ERR", n, False) for n in (1, 2, 3)]
    assert all(want - 0.3 <= got <= want + 2 for got, want in zip(gaps(miss), [1, 2])), gaps(miss)
    assert summary([a for a in attempts if a["destination_id"] == "once"]) == [(single, "once", "failed", "ERR", 1, False)]


def test_exponential_retry_policy(make_hub, queues):
    hub = make_hub(RETRY_INTERVAL_SECONDS=1, MAX_RETRY_LIMIT=2)
    tid = hub.tenant()
    hub.sqs_destination(tid, queues.missing(), topics=["user.deleted"], id="exp")
    event_id = "x-" + uid()
    hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.deleted", "data": {}})
    hub.wait_attempts(tid, 3, timeout=15)
    time.sleep(5)
    attempts = hub.attempts(tid)
    assert summary(attempts) == [(event_id, "exp", "failed", "ERR", n, False) for n in (1, 2, 3)]
    assert all(want - 0.3 <= got <= want + 2 for got, want in zip(gaps(attempts), [1, 2])), gaps(attempts)


def test_manual_retry(make_hub, queues):
    hub = make_hub(RETRY_SCHEDULE="1")
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics=["user.created"], id="ok")
    event_id = "m-" + uid()
    hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.created", "data": {"m": 1}, "metadata": {"k": "v"}})
    receive(url, 1)
    hub.wait_attempts(tid, 1)
    hub.wait_events(tid, 1)
    same(hub.ok("POST", "/retry", {"event_id": event_id, "destination_id": "ok"}, status=202), {"success": True})
    [message] = receive(url, 1)
    assert message["Body"] == '{"m":1}'
    meta = metadata_attribute(message)
    assert (meta["event-id"], meta["topic"], meta["k"]) == (event_id, "user.created", "v")
    hub.wait_attempts(tid, 2)
    same(hub.ok("POST", "/retry", {"event_id": event_id, "destination_id": "ok"}, status=202), {"success": True})
    assert [m["Body"] for m in receive(url, 1)] == ['{"m":1}']
    attempts = hub.wait_attempts(tid, 3)
    assert summary(attempts) == [(event_id, "ok", "success", "OK", 1, False), (event_id, "ok", "success", "OK", 2, True),
                                 (event_id, "ok", "success", "OK", 3, True)]
    with hub.pg() as conn:
        rows = conn.execute("select attempt_number, manual from attempts where event_id=%s order by attempt_number", (event_id,)).fetchall()
    assert rows == [(1, False), (2, True), (3, True)]
    assert len(hub.ok("GET", f"/events?tenant_id={tid}")["models"]) == 1
    assert_quiet(url)


def test_retry_validation(hub, queues):
    tid = hub.tenant()
    url = queues.create()
    hub.sqs_destination(tid, url, topics="*", id="dest")
    event_id = "v-" + uid()
    hub.publish({"id": event_id, "tenant_id": tid, "topic": "user.created", "data": {}})
    receive(url, 1)
    hub.wait_attempts(tid, 1)
    hub.wait_events(tid, 1)
    hub.sqs_destination(tid, url, topics="*", id="later")
    hub.sqs_destination(tid, url, topics=["user.deleted"], id="mismatch")
    hub.sqs_destination(tid, url, topics="*", id="off")
    hub.ok("PUT", f"/tenants/{tid}/destinations/off/disable")
    other = hub.tenant()
    hub.sqs_destination(other, url, topics="*", id="foreign")

    cases = [
        (None, b"{", 422, "invalid JSON", None),
        (None, b"", 422, "invalid JSON", None),
        ({"destination_id": "dest"}, None, 422, "validation error", ["event_id is required"]),
        ({"event_id": event_id}, None, 422, "validation error", ["destination_id is required"]),
        ({"event_id": "absent", "destination_id": "dest"}, None, 404, "event not found", None),
        ({"event_id": event_id, "destination_id": "absent"}, None, 404, "destination not found", None),
        ({"event_id": event_id, "destination_id": "foreign"}, None, 404, "destination not found", None),
        ({"event_id": event_id, "destination_id": "later"}, None, 400, "event has no attempt for this destination", None),
        ({"event_id": event_id, "destination_id": "mismatch"}, None, 400, "destination does not match event", None),
        ({"event_id": event_id, "destination_id": "off"}, None, 400, "Destination is disabled", {"error": "destination_disabled"}),
    ]
    for body, raw, status, message, data in cases:
        try:
            error(hub.request("POST", "/retry", body, raw=raw), status, message, data)
        except AssertionError as e:
            raise AssertionError(f"{body or raw}: {e}") from None
    hub.ok("PATCH", f"/tenants/{tid}/destinations/dest", {"topics": ["user.deleted"]})
    error(hub.request("POST", "/retry", {"event_id": event_id, "destination_id": "dest"}), 400, "destination does not match event")
    hub.ok("PATCH", f"/tenants/{tid}/destinations/dest", {"topics": "*"})
    hub.ok("PUT", f"/tenants/{tid}/destinations/dest/disable")
    error(hub.request("POST", "/retry", {"event_id": event_id, "destination_id": "dest"}), 400, "Destination is disabled",
          {"error": "destination_disabled"})
    hub.ok("PUT", f"/tenants/{tid}/destinations/dest/enable")
    jwt = "Bearer " + hub.ok("GET", f"/tenants/{tid}/token")["token"]
    same(hub.ok("POST", "/retry", {"event_id": event_id, "destination_id": "dest"}, status=202, auth=jwt), {"success": True})
    receive(url, 1)
    other_jwt = "Bearer " + hub.ok("GET", f"/tenants/{other}/token")["token"]
    error(hub.request("POST", "/retry", {"event_id": event_id, "destination_id": "foreign"}, auth=other_jwt), 404, "event not found")
    attempts = hub.wait_attempts(tid, 2)
    assert summary(attempts) == [(event_id, "dest", "success", "OK", 1, False), (event_id, "dest", "success", "OK", 2, True)]
    assert_quiet(url, wait=1)


def test_auto_disable_after_consecutive_failures(make_hub, queues):
    hub = make_hub(RETRY_SCHEDULE="1,2", ALERT_CONSECUTIVE_FAILURE_COUNT=3, ALERT_AUTO_DISABLE_DESTINATION="true")
    tid = hub.tenant()
    good = queues.create()
    hub.sqs_destination(tid, good, topics=["user.created"], id="good")
    hub.sqs_destination(tid, queues.missing(), topics=["user.deleted"], id="bad")
    failing = "f-" + uid()
    start = time.time()
    hub.publish({"id": failing, "tenant_id": tid, "topic": "user.deleted", "data": {}})
    deadline = time.time() + 20
    while hub.ok("GET", f"/tenants/{tid}/destinations/bad").get("disabled_at") is None and time.time() < deadline:
        time.sleep(0.5)
    dest = hub.ok("GET", f"/tenants/{tid}/destinations/bad")
    assert dest.get("disabled_at") is not None
    disabled_at = parse_time(dest.get("disabled_at")).timestamp()
    bad_attempts = hub.wait_attempts(tid, 3, "&destination_id=bad")
    assert summary(bad_attempts) == [(failing, "bad", "failed", "ERR", n, False) for n in (1, 2, 3)]
    assert disabled_at >= parse_time(bad_attempts[-1]["time"]).timestamp() - 0.01
    assert start <= disabled_at <= time.time()
    kv = hub.engines.valkey()
    record = kv.hgetall(hub.key(f"tenant:{{{tid}}}:destination:bad"))
    assert stored_number(record[b"disabled_at"]) == millis(dest.get("disabled_at"))

    skipped = hub.publish({"tenant_id": tid, "topic": "user.deleted", "data": {}})
    same(skipped["destination_ids"], [])
    same(hub.publish({"tenant_id": tid, "topic": "user.created", "data": {"still": "on"}})["destination_ids"], ["good"])
    assert [m["Body"] for m in receive(good, 1)] == ['{"still":"on"}']
    assert hub.ok("GET", f"/tenants/{tid}/destinations/good").get("disabled_at") is None

    # Re-enabling does not reset the failure streak: the next failure disables it again.
    hub.ok("PUT", f"/tenants/{tid}/destinations/bad/enable")
    again = "g-" + uid()
    same(hub.publish({"id": again, "tenant_id": tid, "topic": "user.deleted", "data": {}, "eligible_for_retry": False})["destination_ids"], ["bad"])
    deadline = time.time() + 10
    while hub.ok("GET", f"/tenants/{tid}/destinations/bad").get("disabled_at") is None and time.time() < deadline:
        time.sleep(0.5)
    assert hub.ok("GET", f"/tenants/{tid}/destinations/bad").get("disabled_at") is not None
    assert summary(hub.wait_attempts(tid, 4, "&destination_id=bad"))[-1] == (again, "bad", "failed", "ERR", 1, False)


def test_success_resets_failure_streak(make_hub, queues):
    hub = make_hub(ALERT_CONSECUTIVE_FAILURE_COUNT=2, ALERT_AUTO_DISABLE_DESTINATION="true")
    tid = hub.tenant()
    base = queues.create("base").rsplit("/", 1)[0]
    name = "flaky-" + uid()
    url = f"{base}/{name}"
    hub.sqs_destination(tid, url, topics="*", id="flaky")

    def publish_and_settle(count):
        hub.publish({"tenant_id": tid, "topic": "user.created", "data": {}, "eligible_for_retry": False})
        hub.wait_attempts(tid, count)

    publish_and_settle(1)
    queues.urls.append(sqs.create_queue(QueueName=name)["QueueUrl"])
    publish_and_settle(2)
    receive(url, 1)
    sqs.delete_queue(QueueUrl=url)
    time.sleep(1)
    publish_and_settle(3)
    time.sleep(1.5)
    assert [a["status"] for a in hub.attempts(tid)] == ["failed", "success", "failed"]
    assert hub.ok("GET", f"/tenants/{tid}/destinations/flaky").get("disabled_at") is None
    publish_and_settle(4)
    deadline = time.time() + 10
    while hub.ok("GET", f"/tenants/{tid}/destinations/flaky").get("disabled_at") is None and time.time() < deadline:
        time.sleep(0.5)
    assert hub.ok("GET", f"/tenants/{tid}/destinations/flaky").get("disabled_at") is not None


def test_failures_without_auto_disable(make_hub, queues):
    hub = make_hub(ALERT_CONSECUTIVE_FAILURE_COUNT=2)
    tid = hub.tenant()
    hub.sqs_destination(tid, queues.missing(), topics="*", id="bad")
    for _ in range(3):
        same(hub.publish({"tenant_id": tid, "topic": "user.created", "data": {}, "eligible_for_retry": False})["destination_ids"], ["bad"])
    hub.wait_attempts(tid, 3)
    time.sleep(1.5)
    assert hub.ok("GET", f"/tenants/{tid}/destinations/bad").get("disabled_at") is None
    assert [a["status"] for a in hub.attempts(tid)] == ["failed"] * 3


def test_retry_delay_floor_is_log_batch_threshold(make_hub, queues):
    hub = make_hub(RETRY_SCHEDULE="1,1", LOG_BATCH_THRESHOLD_SECONDS=3)
    tid = hub.tenant()
    hub.sqs_destination(tid, queues.missing(), topics="*", id="slow")
    hub.publish({"tenant_id": tid, "topic": "user.created", "data": {}})
    attempts = hub.wait_attempts(tid, 3, timeout=25)
    assert [a["attempt_number"] for a in attempts] == [1, 2, 3]
    assert all(2.7 <= g <= 6 for g in gaps(attempts)), gaps(attempts)
