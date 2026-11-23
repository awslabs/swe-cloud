"""Which events each poll reads: start position, paging, cursor movement and group discovery."""

import time

from forwarder import assert_documents, bodies, config, now_ms, rfc3339_seconds


def test_start_from_skips_older_events(case):
    group = case.group("app", "s")
    now = now_ms()
    case.put(group, "s", (now - 60000, "sixty"), (now - 40000, "forty"), (now - 20000, "twenty"), (now - 10000, "ten"))
    document = config(case.search.endpoint, named={group: {}}, logs={"start_from": rfc3339_seconds(now - 30000)},
                      exporter={"dataset": "from", "namespace": case.namespace})
    started = now_ms()
    case.run(case.write_config("config.yaml", document), case.index("from"), 2)
    events = case.events(group)
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(events[m], "from") for m in ("twenty", "ten")], started)


def test_initial_lookback_is_relative_to_startup(case):
    group = case.group("app", "s")
    now = now_ms()
    case.put(group, "s", (now - 120000, "two minutes"), (now - 60000, "one minute"), (now - 10000, "recent"))
    document = config(case.search.endpoint, named={group: {}}, logs={"initial_lookback": "30s"}, batch=False,
                      exporter={"dataset": "lookback", "namespace": case.namespace})
    started = now_ms()
    case.run(case.write_config("config.yaml", document), case.index("lookback"), 1)
    event = case.events(group)["recent"]
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(event, "lookback")], started)


def test_first_poll_waits_one_interval(case):
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 5000, "waited"))
    document = config(case.search.endpoint, named={group: {}}, logs={"poll_interval": "6s"},
                      exporter={"dataset": "delay", "namespace": case.namespace})
    with case.collector(case.write_config("config.yaml", document)) as program:
        time.sleep(3.5)
        assert case.search.count(case.index("delay")) == 0, "documents appeared before the first poll interval elapsed"
        assert case.search.wait_count(case.index("delay"), 1, 12) == 1
        time.sleep(1)
        assert program.stop() == 0
    assert bodies(case.search.hits(f"*{case.id}*")) == ["waited"]


def test_small_pages_with_storage_read_everything(case):
    group = case.group("app", "a", "b")
    t = now_ms() - 20000
    case.put(group, "a", *[(t + i * 1000, f"page-a-{i}") for i in range(4)])
    case.put(group, "b", *[(t + 500 + i * 1000, f"page-b-{i}") for i in range(3)])
    document = config(case.search.endpoint, named={group: {}}, storage=case.storage(), logs={"max_events_per_request": 2},
                      exporter={"dataset": "paged", "namespace": case.namespace})
    started = now_ms()
    case.run(case.write_config("config.yaml", document), case.index("paged"), 7)
    expected = [case.expected(event, "paged") for event in case.events(group).values()]
    assert len(expected) == 7
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_cursor_follows_last_event_then_poll_time(case):
    group = case.group("app", "s")
    start = now_ms()
    case.put(group, "s", (start - 30000, "first"))
    document = config(case.search.endpoint, named={group: {}}, logs={"poll_interval": "5s"},
                      exporter={"dataset": "cursor", "namespace": case.namespace})
    index = case.index("cursor")
    with case.collector(case.write_config("config.yaml", document)) as program:
        assert case.search.wait_count(index, 1, 12) == 1
        # Newer than the last event read but older than that poll: the next poll still reads it.
        case.put(group, "s", (start - 25000, "after last event"))
        assert case.search.wait_count(index, 2, 8) == 2
        time.sleep(6)  # at least one poll that finds nothing
        case.put(group, "s", (start - 20000, "older than an empty poll"))
        case.put(group, "s", (now_ms(), "fresh"))
        case.search.wait_count(index, 3, 10)
        time.sleep(6)
        assert program.stop() == 0
    events = case.events(group)
    expected = [case.expected(events[m], "cursor") for m in ("first", "after last event", "fresh")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, start)


def test_older_event_arriving_late_is_skipped(case):
    group = case.group("app", "s")
    start = now_ms()
    case.put(group, "s", (start - 20000, "read first"))
    document = config(case.search.endpoint, named={group: {}}, exporter={"dataset": "late", "namespace": case.namespace})
    index = case.index("late")
    with case.collector(case.write_config("config.yaml", document)) as program:
        assert case.search.wait_count(index, 1, 10) == 1
        case.put(group, "s", (start - 25000, "older than read"))
        time.sleep(3)
        case.put(group, "s", (now_ms(), "newer"))
        case.search.wait_count(index, 2, 10)
        time.sleep(3)
        assert program.stop() == 0
    events = case.events(group)
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(events[m], "late") for m in ("read first", "newer")], start)


def test_named_group_created_after_startup(case):
    present = case.group("present", "s")
    missing = case.name("later")
    case.put(present, "s", (now_ms() - 10000, "present early"))
    document = config(case.search.endpoint, named={present: {}, missing: {}},
                      exporter={"dataset": "created", "namespace": case.namespace})
    index = case.index("created")
    started = now_ms()
    with case.collector(case.write_config("config.yaml", document)) as program:
        assert case.search.wait_count(index, 1, 10) == 1
        time.sleep(2)
        case.group("later", "s")
        case.put(missing, "s", (now_ms(), "later fresh"))
        case.put(present, "s", (now_ms(), "present fresh"))
        case.search.wait_count(index, 3, 10)
        time.sleep(3)
        assert program.running(), program.log()
        assert program.stop() == 0
    events = {**case.events(present), **case.events(missing)}
    expected = [case.expected(events[m], "created") for m in ("present early", "present fresh", "later fresh")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_autodiscover_by_prefix(case):
    a = case.group("d/a", "one", "two")
    b = case.group("d/b", "three")
    outside = case.group("e/c", "four")
    t = now_ms() - 20000
    case.put(a, "one", (t, "a-one"))
    case.put(a, "two", (t + 1, "a-two"))
    case.put(b, "three", (t + 2, "b-three"))
    case.put(outside, "four", (t + 3, "outside"))
    document = config(case.search.endpoint, autodiscover={"prefix": case.name("d/")},
                      exporter={"dataset": "disc", "namespace": case.namespace})
    started = now_ms()
    case.run(case.write_config("config.yaml", document), case.index("disc"), 3)
    events = {**case.events(a), **case.events(b)}
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(e, "disc") for e in events.values()], started)


def test_autodiscover_limit_is_reapplied_each_poll(case):
    a = case.group("d/a", "keep", "drop")
    b = case.group("d/b", "keep")
    c = case.group("d/c", "keep")
    t = now_ms() - 20000
    case.put(a, "keep", (t, "a-keep"))
    case.put(a, "drop", (t + 1, "a-drop"))
    case.put(b, "keep", (t + 2, "b-keep"))
    case.put(c, "keep", (t + 3, "c-keep"))
    events = {**case.events(a), **case.events(b), **case.events(c)}
    document = config(case.search.endpoint, autodiscover={"prefix": case.name("d/"), "limit": 2, "streams": {"names": ["keep"]}},
                      exporter={"dataset": "limit", "namespace": case.namespace})
    index = case.index("limit")
    started = now_ms()
    with case.collector(case.write_config("config.yaml", document)) as program:
        assert case.search.wait_count(index, 2, 10) == 2
        time.sleep(2.5)
        assert bodies(case.search.hits(index)) == ["a-keep", "b-keep"]
        case.delete_group(a)  # frees a discovery slot
        case.search.wait_count(index, 3, 8)
        case.put(b, "keep", (now_ms(), "b-later"))
        case.search.wait_count(index, 4, 8)
        time.sleep(2.5)
        assert program.stop() == 0
    events.update(case.events(b))
    expected = [case.expected(events[m], "limit") for m in ("a-keep", "b-keep", "c-keep", "b-later")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)
