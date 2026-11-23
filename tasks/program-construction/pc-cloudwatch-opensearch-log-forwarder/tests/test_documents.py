"""How CloudWatch events become OpenSearch documents and which index receives them."""

import datetime
import time

from forwarder import assert_documents, bodies, config, now_ms


def utc_labels(start_ms, fmt):
    """Every wall-clock label `fmt` can take between start_ms and now (UTC)."""
    labels = set()
    moment = start_ms // 1000 - 1
    while moment <= time.time() + 1:
        labels.add(datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).strftime(fmt))
        moment += 60
    labels.add(datetime.datetime.now(datetime.timezone.utc).strftime(fmt))
    return labels


def test_document_shape_for_each_event(case):
    group = case.group("app", "s1", "Web")
    other = case.group("other", "s1")
    second = (now_ms() // 1000 - 20) * 1000
    case.put(group, "s1", (second, "plain"), (second + 1, "héllo ✓\nline2\ttab"), (second + 120, '{"k":1}'))
    case.put(group, "Web", (second + 1000, "web line"))
    case.put(other, "s1", (second + 50, "not configured"))
    path = case.write_config("config.yaml", config(case.search.endpoint, named={group: {}},
                                                   exporter={"dataset": "shape", "namespace": case.namespace}))
    started = now_ms()
    case.run(path, case.index("shape"), 4)
    expected = [case.expected(event, "shape") for event in case.events(group).values()]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_named_groups_and_stream_names(case):
    first = case.group("first", "a", "b")
    second = case.group("second", "keep", "also", "drop")
    ignored = case.group("ignored", "keep")
    t = now_ms() - 30000
    case.put(first, "a", (t, "first-a-1"), (t + 10, "first-a-2"))
    case.put(first, "b", (t + 5, "first-b"))
    case.put(second, "keep", (t + 1, "second-keep"))
    case.put(second, "also", (t + 2, "second-also"))
    case.put(second, "drop", (t + 3, "second-drop"))
    case.put(ignored, "keep", (t + 4, "ignored-keep"))
    document = config(case.search.endpoint, named={first: {}, second: {"names": ["keep", "also"]}},
                      exporter={"dataset": "named", "namespace": case.namespace})
    started = now_ms()
    case.run(case.write_config("config.yaml", document), case.index("named"), 5)
    events = {**case.events(first), **case.events(second)}
    expected = [case.expected(events[m], "named") for m in ("first-a-1", "first-a-2", "first-b", "second-keep", "second-also")]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_default_dataset_and_namespace(case):
    default = "ss4o_logs-default-namespace"
    case.search.request("DELETE", "/" + default)
    case.cleanups.append(lambda: case.search.request("DELETE", "/" + default))
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 10000, "defaults"))
    document = config(case.search.endpoint, named={group: {}})
    started = now_ms()
    case.run(case.write_config("config.yaml", document), default, 1)
    event = case.events(group)["defaults"]
    assert_documents(case.search.hits(default), [case.expected(event, "default", "namespace")], started)


def test_logs_index_placeholders_and_fallback(case):
    group = case.group("app", "alpha", "beta", ".hidden", "a..b", "Upper")
    t = now_ms() - 20000
    for offset, stream in enumerate(("alpha", "beta", ".hidden", "a..b", "Upper")):
        case.put(group, stream, (t + offset, f"from {stream}"))
    prefix = f"ix{case.id}"
    exporter = {
        "dataset": "placed",
        "namespace": case.namespace,
        "logs_index": prefix + "-%{cloudwatch.log.stream}-%{no.such.key}-%{scope.version}",
        "logs_index_fallback": "fb",
        "logs_index_time_format": "yyyy.MM.dd",
    }
    started = now_ms()
    case.run(case.write_config("config.yaml", config(case.search.endpoint, named={group: {}}, exporter=exporter)), prefix + "*", 4)
    hits = case.search.hits(prefix + "*")
    days = {index.rsplit("-", 1)[-1] for index, _ in hits}
    assert len(days) == 1 and days <= utc_labels(started, "%Y.%m.%d"), days
    day = days.pop()
    events = case.events(group)
    target = {"alpha": "alpha", "beta": "beta", ".hidden": "fb", "a..b": "fb"}
    expected = [
        case.expected(events[f"from {stream}"], "placed", index=f"{prefix}-{segment}-fb-0.163.0-{day}")
        for stream, segment in target.items()
    ]
    assert_documents(hits, expected, started)
    # The uppercase stream yields an index name OpenSearch refuses; nothing else is created.
    assert set(case.search.indices(f"*{case.id}*")) == {index for index, _ in expected}


def test_default_fallback_and_export_time_suffix(case):
    group = case.group("app", "s")
    old = now_ms() - 3 * 86400 * 1000
    case.put(group, "s", (old, "three days old"), (old + 1, "also old"))
    prefix = f"iy{case.id}"
    exporter = {
        "dataset": "suffix",
        "namespace": case.namespace,
        "logs_index": prefix + "-%{aws.region}_%{nope}.%{cloud.account.id}",
        "logs_index_time_format": "yy-MM-dd_HH",
    }
    started = now_ms()
    case.run(case.write_config("config.yaml", config(case.search.endpoint, named={group: {}}, exporter=exporter)), prefix + "*", 2)
    hits = case.search.hits(prefix + "*")
    names = {index for index, _ in hits}
    allowed = {f"{prefix}-us-east-1_unknown.{case.account}-{label}" for label in utc_labels(started, "%y-%m-%d_%H")}
    assert len(names) == 1 and names <= allowed, names
    (name,) = names
    events = case.events(group).values()
    assert_documents(hits, [case.expected(event, "suffix", index=name) for event in events], started)


def test_time_suffix_on_default_index(case):
    group = case.group("app", "s")
    old = now_ms() - 3 * 86400 * 1000
    case.put(group, "s", (old, "dated by export"))
    exporter = {"dataset": "daily", "namespace": case.namespace, "logs_index_time_format": "yyyy.MM.dd"}
    started = now_ms()
    case.run(case.write_config("config.yaml", config(case.search.endpoint, named={group: {}}, exporter=exporter)),
             case.index("daily") + "-*", 1)
    hits = case.search.hits(f"*{case.id}*")
    names = {index for index, _ in hits}
    allowed = {f"{case.index('daily')}-{label}" for label in utc_labels(started, "%Y.%m.%d")}
    assert len(names) == 1 and names <= allowed, names
    (name,) = names
    event = case.events(group)["dated by export"]
    assert_documents(hits, [case.expected(event, "daily", index=name)], started)


def test_logs_index_record_attribute(case):
    group = case.group("app", "s")
    t = now_ms() - 10000
    case.put(group, "s", (t, "one"), (t + 1, "two"))
    prefix = f"ev{case.id}"
    exporter = {"dataset": "rec", "namespace": case.namespace, "logs_index": prefix + "-%{id}"}
    started = now_ms()
    case.run(case.write_config("config.yaml", config(case.search.endpoint, named={group: {}}, exporter=exporter)), prefix + "*", 2)
    expected = [case.expected(event, "rec", index=f"{prefix}-{event['eventId']}") for event in case.events(group).values()]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_bulk_action_against_data_stream_and_index(case):
    template = f"ds{case.id}"
    status, body = case.search.request(
        "PUT", f"/_index_template/{template}",
        {"index_patterns": [f"ss4o_logs-ds*-{case.namespace}"], "data_stream": {}, "priority": 500},
    )
    assert status == 200, body
    case.cleanups.append(lambda: case.search.request("DELETE", f"/_index_template/{template}"))
    group = case.group("app", "s")
    t = now_ms() - 10000
    case.put(group, "s", (t, "bulk one"), (t + 1, "bulk two"))
    events = case.events(group).values()

    def run(dataset, action, expected_count):
        exporter = {"dataset": dataset, "namespace": case.namespace, "bulk_action": action, "retry_on_failure": {"enabled": False}}
        document = config(case.search.endpoint, named={group: {}}, exporter=exporter)
        started = now_ms()
        case.run(case.write_config(f"{dataset}.yaml", document), case.index(dataset), expected_count or 1,
                 timeout=20 if expected_count else 5, settle=3 if expected_count else 0)
        return started

    run("dsindex", "index", 0)
    assert case.search.hits(case.index("dsindex")) == []
    started = run("dscreate", "create", 2)
    backing = f".ds-{case.index('dscreate')}-000001"
    assert_documents(case.search.hits(case.index("dscreate")), [case.expected(e, "dscreate", index=backing) for e in events], started)
    started = run("plainindex", "index", 2)
    assert_documents(case.search.hits(case.index("plainindex")), [case.expected(e, "plainindex") for e in events], started)


def test_ingest_pipeline(case):
    pipeline = f"tag{case.id}"
    status, body = case.search.request("PUT", f"/_ingest/pipeline/{pipeline}",
                                       {"processors": [{"set": {"field": "pipeline_tag", "value": f"p-{case.id}"}}]})
    assert status == 200, body
    case.cleanups.append(lambda: case.search.request("DELETE", f"/_ingest/pipeline/{pipeline}"))
    group = case.group("app", "s")
    case.put(group, "s", (now_ms() - 10000, "through pipeline"))
    event = case.events(group)["through pipeline"]

    exporter = {"dataset": "piped", "namespace": case.namespace, "pipeline": pipeline}
    started = now_ms()
    case.run(case.write_config("piped.yaml", config(case.search.endpoint, named={group: {}}, exporter=exporter)), case.index("piped"), 1)
    index, document = case.expected(event, "piped")
    assert_documents(case.search.hits(case.index("piped")), [(index, {**document, "pipeline_tag": f"p-{case.id}"})], started)

    exporter = {"dataset": "unpiped", "namespace": case.namespace, "pipeline": f"absent{case.id}", "retry_on_failure": {"enabled": False}}
    case.run(case.write_config("unpiped.yaml", config(case.search.endpoint, named={group: {}}, exporter=exporter)),
             case.index("unpiped"), 1, timeout=5, settle=0)
    assert bodies(case.search.hits(case.index("unpiped"))) == []
    assert set(case.search.indices(f"*{case.id}*")) == {case.index("piped")}
