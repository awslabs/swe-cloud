"""Invocation: OTLP transports, environment configuration, option errors and shutdown."""

from emf_check import assert_documents, emf
from emf_driver import (DELTA, free_ports, gauge, histogram, histogram_point, now_ms, number_point, request,
                        resource_metrics, sum_metric, summary, summary_point, wait_events)
from harness import run_program


def test_http_protobuf_http_json_and_grpc_are_equivalent(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000

    def req(via):
        return request(resource_metrics({"service.name": "svc", "via": via}, [
            gauge("g", [number_point(1.25, t, {"k": 3})], unit="ms"),
            sum_metric("d", [number_point(4, t)], temporality=DELTA),
            histogram("h", [histogram_point(t, 3, 6.0, 1.0, 3.0, {"op": "x"}, bounds=[1.0, 2.0], buckets=[1, 1, 1])]),
            summary("s", [summary_point(t, 2, 4.0)]),
        ]))

    assert f.json(req("json")) == 200
    f.grpc(req("grpc"))
    assert f.protobuf(req("proto")) == 200
    expected = []
    for via in ("json", "grpc", "proto"):
        base = {"service.name": "svc", "via": via}
        expected += [
            emf(t, "svc", ["k", "service.name", "via"], {"g": "Milliseconds"}, {**base, "k": "3", "g": 1.25}),
            emf(t, "svc", ["service.name", "via"], {"d": "Count"}, {**base, "d": 4}),
            emf(t, "svc", ["op", "service.name", "via"], {"h": "Count"},
                {**base, "op": "x", "h": {"Count": 3, "Sum": 6.0, "Min": 1.0, "Max": 3.0}}),
        ]
    assert_documents(wait_events(group, stream, 9), expected)


def _environment(group, stream, http_port, grpc_port):
    return {"ROTEL_EXPORTER": "awsemf", "ROTEL_OTLP_HTTP_ENDPOINT": f"127.0.0.1:{http_port}",
            "ROTEL_OTLP_GRPC_ENDPOINT": f"127.0.0.1:{grpc_port}",
            "ROTEL_AWSEMF_EXPORTER_CUSTOM_ENDPOINT": "http://engines:4566",
            "ROTEL_AWSEMF_EXPORTER_REGION": "us-east-1",
            "ROTEL_AWSEMF_EXPORTER_LOG_GROUP_NAME": group, "ROTEL_AWSEMF_EXPORTER_LOG_STREAM_NAME": stream,
            "ROTEL_AWSEMF_EXPORTER_NAMESPACE": "EnvNs", "ROTEL_AWSEMF_EXPORTER_EXCLUDE_DIMENSIONS": "a,b*",
            "ROTEL_AWSEMF_EXPORTER_RETAIN_INITIAL_VALUE_OF_DELTA_METRIC": "true"}


def test_configuration_from_environment(log_target, forwarder):
    group, stream = log_target
    ports = free_ports()
    f = forwarder(env=_environment(group, stream, *ports), endpoint_flags=False, ports=ports)
    t = now_ms() - 2000
    assert f.protobuf(request(resource_metrics({"service.name": "svc"},
                                               [sum_metric("c", [number_point(4, t, {"a": "1", "bee": "2",
                                                                                     "c": "3"})])]))) == 200
    assert_documents(wait_events(group, stream, 1), [
        emf(t, "EnvNs", ["c", "service.name"], {"c": "Count"},
            {"service.name": "svc", "a": "1", "bee": "2", "c": 4}),
    ])


def test_options_override_environment(log_target, forwarder):
    group, stream = log_target
    ports = free_ports()
    f = forwarder(None, None, "--awsemf-exporter-namespace", "CliNs", "--awsemf-exporter-exclude-dimensions", "c",
                  env=_environment(group, stream, *ports), endpoint_flags=False, ports=ports)
    t = now_ms() - 2000
    assert f.protobuf(request(resource_metrics({"service.name": "svc"},
                                               [gauge("c", [number_point(4, t, {"a": "1", "bee": "2",
                                                                                "c": "3"})])]))) == 200
    assert_documents(wait_events(group, stream, 1), [
        emf(t, "CliNs", ["a", "bee", "service.name"], {"c": "Count"},
            {"service.name": "svc", "a": "1", "bee": "2", "c": 4}),
    ])


def test_sigterm_stops_cleanly(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 2000
    assert f.protobuf(request(resource_metrics({"service.name": "svc"}, [gauge("g", [number_point(1, t)])]))) == 200
    assert_documents(wait_events(group, stream, 1, settle=0), [
        emf(t, "svc", ["service.name"], {"g": "Count"}, {"service.name": "svc", "g": 1}),
    ])
    assert f.stop() == 0


def test_invalid_option_values_are_rejected():
    for args in (["start", "--exporter", "bogus"], ["start", "--awsemf-exporter-log-retention", "abc"]):
        result = run_program(args, timeout=15)
        assert result.returncode != 0, (args, result.stdout, result.stderr)
        assert result.stderr.strip(), args
