"""Read positions across restarts, with and without the storage extension."""

import signal
import time

import pytest

from forwarder import assert_documents, bodies, config, now_ms, rfc3339_seconds


def test_restart_without_storage_reads_again(case):
    group = case.group("app", "s")
    t = now_ms() - 30000
    case.put(group, "s", (t, "old one"), (t + 1000, "old two"))
    path = case.write_config("config.yaml", config(case.search.endpoint, named={group: {}},
                                                   exporter={"dataset": "nostore", "namespace": case.namespace}))
    index = case.index("nostore")
    started = now_ms()
    case.run(path, index, 2)
    assert bodies(case.search.hits(index)) == ["old one", "old two"]
    case.put(group, "s", (now_ms(), "new"))
    case.run(path, index, 5)
    events = case.events(group)
    expected = [case.expected(events[m], "nostore") for m in ("old one", "old one", "old two", "old two", "new")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_restart_with_storage_resumes(case):
    group = case.group("app", "s")
    t = now_ms() - 30000
    case.put(group, "s", (t, "before one"), (t + 1000, "before two"))
    path = case.write_config("config.yaml", config(case.search.endpoint, named={group: {}}, storage=case.storage(),
                                                   exporter={"dataset": "store", "namespace": case.namespace}))
    index = case.index("store")
    started = now_ms()
    with case.collector(path) as program:
        assert case.search.wait_count(index, 2, 10) == 2
        case.put(group, "s", (t + 5000, "late while running"))
        time.sleep(3)
        assert program.stop() == 0
    assert bodies(case.search.hits(index)) == ["before one", "before two"]
    time.sleep(1.5)
    case.put(group, "s", (t + 10000, "late while stopped"))
    case.put(group, "s", (now_ms(), "after restart"))
    case.run(path, index, 3, settle=4)
    events = case.events(group)
    expected = [case.expected(events[m], "store") for m in ("before one", "before two", "after restart")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_fresh_storage_directory_starts_over(case):
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 20000, "only event"))
    storage_a, storage_b = case.storage("a"), case.storage("b")
    exporter = {"dataset": "fresh", "namespace": case.namespace}
    first = case.write_config("a.yaml", config(case.search.endpoint, named={group: {}}, storage=storage_a, exporter=exporter))
    second = case.write_config("b.yaml", config(case.search.endpoint, named={group: {}}, storage=storage_b, exporter=exporter))
    index = case.index("fresh")
    started = now_ms()
    case.run(first, index, 1)
    case.run(first, index, 2, timeout=4, settle=0)
    assert case.search.count(index) == 1
    case.run(second, index, 2)
    event = case.events(group)["only event"]
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(event, "fresh")] * 2, started)


def test_position_is_kept_per_group_not_per_stream_filter(case):
    group = case.group("app", "one", "two")
    t = now_ms() - 20000
    case.put(group, "one", (t, "one early"))
    case.put(group, "two", (t + 1000, "two early"))
    storage = case.storage()
    exporter = {"dataset": "filter", "namespace": case.namespace}
    first = case.write_config("one.yaml", config(case.search.endpoint, named={group: {"names": ["one"]}}, storage=storage, exporter=exporter))
    second = case.write_config("two.yaml", config(case.search.endpoint, named={group: {"names": ["two"]}}, storage=storage, exporter=exporter))
    index = case.index("filter")
    started = now_ms()
    case.run(first, index, 1)
    time.sleep(1.5)
    case.put(group, "two", (now_ms(), "two later"))
    case.run(second, index, 2, settle=4)
    events = case.events(group)
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(events[m], "filter") for m in ("one early", "two later")], started)


def test_group_added_on_restart_starts_from_beginning(case):
    g1 = case.group("g1", "s")
    g2 = case.group("g2", "s")
    t = now_ms() - 20000
    case.put(g1, "s", (t, "g1 old"))
    case.put(g2, "s", (t + 1000, "g2 old"))
    storage = case.storage()
    exporter = {"dataset": "added", "namespace": case.namespace}
    first = case.write_config("one.yaml", config(case.search.endpoint, named={g1: {}}, storage=storage, exporter=exporter))
    both = case.write_config("both.yaml", config(case.search.endpoint, named={g1: {}, g2: {}}, storage=storage, exporter=exporter))
    index = case.index("added")
    started = now_ms()
    with case.collector(first) as program:
        assert case.search.wait_count(index, 1, 10) == 1
        time.sleep(2)
        assert program.stop(signal.SIGINT) == 0
    time.sleep(1.5)
    case.put(g1, "s", (now_ms(), "g1 new"))
    case.run(both, index, 3, settle=4)
    events = {**case.events(g1), **case.events(g2)}
    expected = [case.expected(events[m], "added") for m in ("g1 old", "g2 old", "g1 new")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_later_start_from_overrides_saved_position(case):
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 20000, "first run"))
    storage = case.storage()
    exporter = {"dataset": "override", "namespace": case.namespace}
    index = case.index("override")
    first = case.write_config("first.yaml", config(case.search.endpoint, named={group: {}}, storage=storage, exporter=exporter))
    started = now_ms()
    case.run(first, index, 1, settle=1.5)
    time.sleep(1.5)
    case.put(group, "s", (now_ms(), "between runs"))
    time.sleep(2.2)
    case.put(group, "s", (now_ms(), "after start_from"))
    events = case.events(group)
    boundary = rfc3339_seconds(events["after start_from"]["timestamp"])
    assert rfc3339_seconds(events["between runs"]["timestamp"]) < boundary
    second = case.write_config("second.yaml", config(case.search.endpoint, named={group: {}}, storage=storage,
                                                     logs={"start_from": boundary}, exporter=exporter))
    case.run(second, index, 2, settle=4)
    expected = [case.expected(events[m], "override") for m in ("first run", "after start_from")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


@pytest.mark.parametrize("start", [{"initial_lookback": "1h"}, {"start_from": "2000-01-01T00:00:00Z"}], ids=["initial_lookback", "start_from"])
def test_saved_position_overrides_earlier_start_option(case, start):
    group = case.group("app", "s")
    t = now_ms() - 30000
    case.put(group, "s", (t, "old one"), (t + 1000, "old two"))
    path = case.write_config("config.yaml", config(case.search.endpoint, named={group: {}}, storage=case.storage(), logs=start,
                                                   exporter={"dataset": "earlier", "namespace": case.namespace}))
    index = case.index("earlier")
    started = now_ms()
    case.run(path, index, 2)
    time.sleep(1.5)
    case.put(group, "s", (now_ms(), "new"))
    case.run(path, index, 3, settle=4)
    events = case.events(group)
    expected = [case.expected(events[m], "earlier") for m in ("old one", "old two", "new")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)
