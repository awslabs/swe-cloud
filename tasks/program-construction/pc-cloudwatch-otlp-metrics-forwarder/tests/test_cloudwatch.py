"""CloudWatch Logs effects: group/stream creation, retention, preserved data and dropped requests."""

import time

from emf_check import assert_documents, emf
from emf_driver import (delete_group, describe_group, gauge, logs_client, now_ms, number_point, read_events,
                        request, resource_metrics, stream_names, sum_metric, wait_events)
from opentelemetry.proto.metrics.v1 import metrics_pb2 as mp

SVC = {"service.name": "svc"}


def single(t, value=1):
    return emf(t, "svc", ["service.name"], {"g": "Count"}, {"service.name": "svc", "g": value})


def test_creates_missing_group_and_stream_with_retention(fresh_target, forwarder):
    # Adapted from mod.rs resource_not_found_triggers_creation, against real CloudWatch Logs state.
    group, stream = fresh_target
    f = forwarder(group, stream, "--awsemf-exporter-log-retention", "7")
    t = now_ms() - 2000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(1, t)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [single(t)])
    info = describe_group(group)
    assert info is not None and info.get("retentionInDays") == 7, info
    assert stream_names(group) == [stream]


def test_creates_missing_group_without_retention_by_default(fresh_target, forwarder):
    group, stream = fresh_target
    f = forwarder(group, stream)
    t = now_ms() - 2000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(1, t)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [single(t)])
    info = describe_group(group)
    assert info is not None and "retentionInDays" not in info, info
    assert stream_names(group) == [stream]


def test_existing_group_gets_stream_but_keeps_its_retention(fresh_target, forwarder):
    group, stream = fresh_target
    logs = logs_client()
    logs.create_log_group(logGroupName=group)
    logs.create_log_stream(logGroupName=group, logStreamName="other")
    logs.put_log_events(logGroupName=group, logStreamName="other",
                        logEvents=[{"timestamp": now_ms() - 10_000, "message": "keep me"}])
    f = forwarder(group, stream, "--awsemf-exporter-log-retention", "14")
    t = now_ms() - 2000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(1, t)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [single(t)])
    info = describe_group(group)
    assert info is not None and "retentionInDays" not in info, info
    assert stream_names(group) == sorted(["other", stream])
    assert [e["message"] for e in read_events(group, "other")] == ["keep me"]


def test_default_names_append_to_existing_stream(forwarder):
    group, stream = "/metrics/default", "otel-stream"
    delete_group(group)
    try:
        logs = logs_client()
        logs.create_log_group(logGroupName=group)
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
        earlier = now_ms() - 60_000
        logs.put_log_events(logGroupName=group, logStreamName=stream,
                            logEvents=[{"timestamp": earlier, "message": "earlier"}])
        f = forwarder()
        t = now_ms() - 2000
        assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(1, t)])]))) == 200
        events = wait_events(group, stream, 2)
        assert [(e["timestamp"], e["message"]) for e in events if e["message"] == "earlier"] == [(earlier, "earlier")]
        assert_documents([e for e in events if e["message"] != "earlier"], [single(t)])
        assert stream_names(group) == [stream]
    finally:
        delete_group(group)


def test_invalid_requests_are_dropped_whole(log_target, forwarder):
    # Adapted from transformer.rs test_number_data_point_missing_value.
    group, stream = log_target
    f = forwarder(group, stream)
    t1 = now_ms() - 3000
    valueless = mp.NumberDataPoint(time_unix_nano=t1 * 1_000_000)
    assert f.protobuf(request(resource_metrics(SVC, [gauge("ok", [number_point(1, t1)]), gauge("bad", [valueless]),
                                                     gauge("after", [number_point(2, t1)])]))) == 200
    time.sleep(1.5)
    t3 = now_ms() - 1000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(3, t3)]),
                                                     sum_metric("c", [number_point(1, t3)])]))) == 200
    assert_documents(wait_events(group, stream, 1, settle=2.5), [single(t3, 3)])


def test_stream_deleted_while_running_is_recreated(log_target, forwarder):
    group, stream = log_target
    logs = logs_client()
    logs.create_log_stream(logGroupName=group, logStreamName="other")
    f = forwarder(group, stream, "--awsemf-exporter-log-retention", "7")
    t1 = now_ms() - 2000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(1, t1)])]))) == 200
    assert_documents(wait_events(group, stream, 1), [single(t1)])
    logs.delete_log_stream(logGroupName=group, logStreamName=stream)
    t2 = now_ms() - 1000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(2, t2)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [single(t2, 2)])
    assert stream_names(group) == sorted(["other", stream])
    info = describe_group(group)
    assert info is not None and "retentionInDays" not in info, info


def test_group_deleted_while_running_is_recreated_with_retention(fresh_target, forwarder):
    group, stream = fresh_target
    f = forwarder(group, stream, "--awsemf-exporter-log-retention", "5")
    t1 = now_ms() - 2000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(1, t1)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [single(t1)])
    delete_group(group)
    assert describe_group(group) is None
    t2 = now_ms() - 1000
    assert f.protobuf(request(resource_metrics(SVC, [gauge("g", [number_point(2, t2)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [single(t2, 2)])
    info = describe_group(group)
    assert info is not None and info.get("retentionInDays") == 5, info
    assert stream_names(group) == [stream]


def test_target_is_created_only_when_something_is_written(fresh_target, forwarder):
    group, stream = fresh_target
    f = forwarder(group, stream, "--awsemf-exporter-log-retention", "3")
    t1 = now_ms() - 3000
    # A first cumulative point produces no document; a valueless point drops its request.
    assert f.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(5, t1)])]))) == 200
    time.sleep(1.5)
    valueless = mp.NumberDataPoint(time_unix_nano=t1 * 1_000_000)
    assert f.protobuf(request(resource_metrics(SVC, [gauge("bad", [valueless])]))) == 200
    time.sleep(4)
    assert describe_group(group) is None
    t2 = now_ms() - 1000
    assert f.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(8, t2)])]))) == 200
    assert_documents(wait_events(group, stream, 1, timeout=30), [
        emf(t2, "svc", ["service.name"], {"c": "Count"}, {"service.name": "svc", "c": 3}),
    ])
    info = describe_group(group)
    assert info is not None and info.get("retentionInDays") == 3, info
    assert stream_names(group) == [stream]
