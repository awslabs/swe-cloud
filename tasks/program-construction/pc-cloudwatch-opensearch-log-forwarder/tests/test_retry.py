"""Export failures: retryable per-document rejections, disabled retry and partially failed requests.

OpenSearch rejects writes to an index carrying `index.blocks.read_only_allow_delete` with a
per-document 429 status, which the exporter treats as retryable.
"""

import time

from forwarder import assert_documents, bodies, config, now_ms


def block(case, index):
    status, body = case.search.request("PUT", f"/{index}", {"settings": {"index.blocks.read_only_allow_delete": True}})
    assert status == 200, body


def unblock(case, index):
    status, body = case.search.request("GET", f"/{index}/_settings?filter_path=*.settings.index.blocks")
    assert status == 200 and body, f"fixture block on {index} vanished before the test lifted it: {body}"
    status, body = case.search.request("PUT", f"/{index}/_settings", {"index.blocks.read_only_allow_delete": None})
    assert status == 200, body


RETRY = {"enabled": True, "initial_interval": "1s", "max_interval": "1s", "max_elapsed_time": "60s"}


def test_rejected_request_is_retried_after_unblock(case):
    group = case.group("app", "s")
    index = case.index("retry")
    block(case, index)
    t = now_ms() - 10000
    case.put(group, "s", (t, "retry first"), (t + 1000, "retry second"))
    document = config(case.search.endpoint, named={group: {}}, exporter={"dataset": "retry", "namespace": case.namespace, "retry_on_failure": RETRY})
    started = now_ms()
    with case.collector(case.write_config("config.yaml", document)) as program:
        time.sleep(5)
        assert case.search.count(index) == 0
        assert program.running(), program.log()
        unblock(case, index)
        case.search.wait_count(index, 1, 10)
        time.sleep(5)
        assert program.stop() == 0
    # Only the first rejected document of the request is carried into the retry.
    event = case.events(group)["retry first"]
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(event, "retry")], started)


def test_disabled_retry_drops_rejected_documents(case):
    group = case.group("app", "s")
    index = case.index("noretry")
    block(case, index)
    case.put(group, "s", (now_ms() - 10000, "dropped"))
    document = config(case.search.endpoint, named={group: {}},
                      exporter={"dataset": "noretry", "namespace": case.namespace, "retry_on_failure": {"enabled": False}})
    started = now_ms()
    with case.collector(case.write_config("config.yaml", document)) as program:
        time.sleep(4)
        unblock(case, index)
        time.sleep(3)
        assert case.search.count(index) == 0
        case.put(group, "s", (now_ms(), "accepted"))
        case.search.wait_count(index, 1, 10)
        time.sleep(3)
        assert program.stop() == 0
    event = case.events(group)["accepted"]
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(event, "noretry")], started)


def test_partial_failure_keeps_accepted_documents(case):
    group = case.group("app", "ok", "blk")
    prefix = f"iz{case.id}"
    block(case, f"{prefix}-blk")
    t = now_ms() - 10000
    case.put(group, "ok", (t, "ok one"), (t + 1000, "ok two"))
    case.put(group, "blk", (t + 500, "blocked one"), (t + 1500, "blocked two"), (t + 2000, "blocked three"))
    exporter = {"dataset": "partial", "namespace": case.namespace, "logs_index": prefix + "-%{cloudwatch.log.stream}", "retry_on_failure": RETRY}
    document = config(case.search.endpoint, named={group: {}}, exporter=exporter)
    started = now_ms()
    with case.collector(case.write_config("config.yaml", document)) as program:
        case.search.wait_count(f"{prefix}-ok", 2, 10)
        time.sleep(4)
        assert bodies(case.search.hits(prefix + "*")) == ["ok one", "ok two"]
        unblock(case, f"{prefix}-blk")
        case.search.wait_count(f"{prefix}-blk", 1, 10)
        time.sleep(5)
        assert program.stop() == 0
    events = case.events(group)
    expected = [
        case.expected(events["ok one"], "partial", index=f"{prefix}-ok"),
        case.expected(events["ok two"], "partial", index=f"{prefix}-ok"),
        case.expected(events["blocked one"], "partial", index=f"{prefix}-blk"),
    ]
    assert_documents(case.search.hits(f"*{case.id}*"), expected, started)


def test_retry_gives_up_after_max_elapsed_time(case):
    group = case.group("app", "s")
    index = case.index("giveup")
    block(case, index)
    case.put(group, "s", (now_ms() - 10000, "abandoned"))
    retry = {"enabled": True, "initial_interval": "1s", "max_interval": "1s", "max_elapsed_time": "3s"}
    document = config(case.search.endpoint, named={group: {}}, exporter={"dataset": "giveup", "namespace": case.namespace, "retry_on_failure": retry})
    started = now_ms()
    with case.collector(case.write_config("config.yaml", document)) as program:
        time.sleep(8)
        unblock(case, index)
        time.sleep(5)
        assert case.search.count(index) == 0
        case.put(group, "s", (now_ms(), "after giving up"))
        case.search.wait_count(index, 1, 10)
        time.sleep(3)
        assert program.stop() == 0
    event = case.events(group)["after giving up"]
    assert_documents(case.search.hits(f"*{case.id}*"), [case.expected(event, "giveup")], started)
