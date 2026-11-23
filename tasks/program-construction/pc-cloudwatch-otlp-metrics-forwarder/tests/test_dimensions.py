"""Dimension include/exclude filtering of the metric directive."""

from emf_check import assert_documents, emf
from emf_driver import gauge, now_ms, number_point, request, resource_metrics, sum_metric, summary, summary_point, \
    wait_events, DELTA


def test_include_and_exclude_patterns(log_target, forwarder):
    # Adapted from dim_filter.rs test_include_and_exclude_combined / wildcard tests.
    group, stream = log_target
    f = forwarder(group, stream,
                  "--awsemf-exporter-include-dimensions", "service.*,http.*,route",
                  "--awsemf-exporter-exclude-dimensions", "service.version,*.internal")
    t = now_ms() - 3000
    res = {"service.name": "a", "service.namespace": "n", "service.version": "1", "host.id": "h",
           "Service.Tier": "x", "zone.internal": "z"}
    dp = {"http.method": "GET", "route": "/r", "http.internal": "y", "other": "o", "routes": "rs"}
    assert f.protobuf(request(resource_metrics(res, [
        gauge("g", [number_point(2.5, t, dp)]),
        sum_metric("d", [number_point(3, t)], temporality=DELTA),
        summary("q", [summary_point(t, 3, 9.0)]),
    ]))) == 200
    res_fields = {"service.name": "a", "service.namespace": "n", "service.version": "1", "host.id": "h",
                  "Service.Tier": "x", "zone.internal": "z"}
    assert_documents(wait_events(group, stream, 2), [
        emf(t, "n/a", ["http.method", "route", "service.name", "service.namespace"], {"g": "Count"},
            {**res_fields, **dp, "g": 2.5}),
        emf(t, "n/a", ["service.name", "service.namespace"], {"d": "Count"}, {**res_fields, "d": 3}),
    ])


def test_exclude_only_pattern(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream, "--awsemf-exporter-exclude-dimensions", "host.*")
    t = now_ms() - 3000
    res = {"service.name": "checkout", "service.namespace": "shop", "host.id": "h-1", "host.name": "box",
           "hostname": "kept"}
    assert f.protobuf(request(resource_metrics(res, [gauge("g", [number_point(1, t, {"route": "/cart"})])]))) == 200
    assert_documents(wait_events(group, stream, 1), [
        emf(t, "shop/checkout", ["hostname", "route", "service.name", "service.namespace"], {"g": "Count"},
            {**res, "route": "/cart", "g": 1}),
    ])


def test_patterns_are_literal_case_sensitive_and_accumulate(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream,
                  "--awsemf-exporter-include-dimensions", "Service.*",
                  "--awsemf-exporter-include-dimensions", "a?c,mid*dle")
    t = now_ms() - 3000
    attrs = {"a?c": "1", "abc": "2", "middle": "3", "mid-x-dle": "4", "midd": "5"}
    assert f.protobuf(request(resource_metrics({"service.name": "svc"}, [gauge("g", [number_point(1, t, attrs)])]))) \
        == 200
    assert_documents(wait_events(group, stream, 1), [
        emf(t, "svc", ["a?c", "mid-x-dle", "middle"], {"g": "Count"}, {"service.name": "svc", **attrs, "g": 1}),
    ])


def test_excluding_every_dimension_leaves_an_empty_set(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream, "--awsemf-exporter-exclude-dimensions", "*")
    t = now_ms() - 3000
    assert f.protobuf(request(resource_metrics({"service.name": "svc"},
                                               [gauge("g", [number_point(1, t, {"k": "v"})])]))) == 200
    assert_documents(wait_events(group, stream, 1), [
        emf(t, "svc", [], {"g": "Count"}, {"service.name": "svc", "k": "v", "g": 1}),
    ])
