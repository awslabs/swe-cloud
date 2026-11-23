"""Single-export OTLP to EMF mapping: document shape, grouping, attributes, namespaces, units, histograms."""

from emf_check import assert_documents, emf
from emf_driver import (CUMULATIVE, DELTA, gauge, histogram, histogram_point, now_ms, number_point, request,
                        resource_metrics, sum_metric, wait_events)

SHOP = {"service.name": "checkout", "service.namespace": "shop", "host.id": "h-1", "res.int": 5}
SHOP_FIELDS = {"service.name": "checkout", "service.namespace": "shop", "host.id": "h-1", "res.int": "5"}
SHOP_DIMS = ["host.id", "res.int", "service.name", "service.namespace"]
CART = {"route": "/cart"}


def test_gauges_and_sums_grouped_by_labels_timestamp_and_type(log_target, forwarder):
    # Adapted from transformer.rs test_grouping_by_labels_and_timestamp / test_different_timestamps_create_separate_groups.
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    assert f.protobuf(request(resource_metrics(SHOP, [
        gauge("temp", [number_point(21.5, t, CART)], unit="s"),
        gauge("hits", [number_point(3, t, CART)], unit="1"),
        sum_metric("reqs", [number_point(4, t, CART)], temporality=DELTA),
        gauge("temp", [number_point(30.0, t, {"route": "/home"})], unit="s"),
        gauge("temp", [number_point(22.0, t + 1, CART)], unit="s"),
    ]))) == 200
    dims = SHOP_DIMS + ["route"]
    cart = {**SHOP_FIELDS, "route": "/cart"}
    assert_documents(wait_events(group, stream, 4), [
        emf(t, "shop/checkout", dims, {"temp": "Seconds", "hits": "Count"}, {**cart, "temp": 21.5, "hits": 3}),
        emf(t, "shop/checkout", dims, {"reqs": "Count"}, {**cart, "reqs": 4}),
        emf(t, "shop/checkout", dims, {"temp": "Seconds"}, {**SHOP_FIELDS, "route": "/home", "temp": 30.0}),
        emf(t + 1, "shop/checkout", dims, {"temp": "Seconds"}, {**cart, "temp": 22.0}),
    ])


def test_equal_label_sets_merge_across_resources_and_scopes(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    res = {"service.name": "svc", "env": "prod"}
    point_attrs = {"k": 3, "flag": False}
    assert f.protobuf(request(
        resource_metrics(res, [gauge("a", [number_point(1, t, point_attrs)], unit="By"),
                               sum_metric("d", [number_point(2.5, t, point_attrs)], temporality=DELTA)], scope="one"),
        resource_metrics(res, [gauge("b", [number_point(2.0, t, point_attrs)])], scope="two"),
        resource_metrics({"service.name": "svc"}, [gauge("b", [number_point(7, t, point_attrs)])], scope="three"),
    )) == 200
    fields = {"service.name": "svc", "env": "prod", "k": "3", "flag": "false"}
    dims = ["env", "flag", "k", "service.name"]
    assert_documents(wait_events(group, stream, 3), [
        emf(t, "svc", dims, {"a": "Bytes", "b": "Count"}, {**fields, "a": 1, "b": 2.0}),
        emf(t, "svc", dims, {"d": "Count"}, {**fields, "d": 2.5}),
        emf(t, "svc", ["flag", "k", "service.name"], {"b": "Count"},
            {"service.name": "svc", "k": "3", "flag": "false", "b": 7}),
    ])


def test_attribute_values_become_string_fields(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    assert f.protobuf(request(resource_metrics(SHOP, [
        gauge("typed", [number_point(1, t, {"i": 5, "b": True, "d": 1.5, "d2": 2.0, "arr": [1, 2], "by": b"xy",
                                            "s": "str"})]),
        # A data point attribute replaces a resource attribute of the same name; the namespace is still
        # derived from the resource.
        gauge("override", [number_point(1, t + 1, {"service.name": "dp-svc"})]),
    ]))) == 200
    assert_documents(wait_events(group, stream, 2), [
        emf(t, "shop/checkout", SHOP_DIMS + ["b", "d", "d2", "i", "s"], {"typed": "Count"},
            {**SHOP_FIELDS, "b": "true", "d": "1.5", "d2": "2", "i": "5", "s": "str", "typed": 1}),
        emf(t + 1, "shop/checkout", SHOP_DIMS, {"override": "Count"}, {**SHOP_FIELDS, "service.name": "dp-svc",
                                                                       "override": 1}),
    ])


def test_namespace_derived_from_service_attributes(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    g = lambda: [gauge("g", [number_point(1, t)])]  # noqa: E731
    assert f.protobuf(request(
        resource_metrics({"service.name": "checkout", "service.namespace": "shop"}, g()),
        resource_metrics({"service.name": "only-svc"}, g()),
        resource_metrics({"service.namespace": "only-ns"}, g()),
        resource_metrics({}, g()),
        resource_metrics({"service.name": 7}, g()),
    )) == 200
    assert_documents(wait_events(group, stream, 5), [
        emf(t, "shop/checkout", ["service.name", "service.namespace"], {"g": "Count"},
            {"service.name": "checkout", "service.namespace": "shop", "g": 1}),
        emf(t, "only-svc", ["service.name"], {"g": "Count"}, {"service.name": "only-svc", "g": 1}),
        emf(t, "only-ns", ["service.namespace"], {"g": "Count"}, {"service.namespace": "only-ns", "g": 1}),
        emf(t, "default", [], {"g": "Count"}, {"g": 1}),
        emf(t, "7", ["service.name"], {"g": "Count"}, {"service.name": "7", "g": 1}),
    ])


def test_namespace_option_overrides_resource(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream, "--awsemf-exporter-namespace", "Fixed/NS")
    t = now_ms() - 3000
    assert f.protobuf(request(
        resource_metrics({"service.name": "a", "service.namespace": "n"}, [gauge("g", [number_point(2.5, t)])]),
        resource_metrics({}, [gauge("g", [number_point(4, t)])]),
    )) == 200
    assert_documents(wait_events(group, stream, 2), [
        emf(t, "Fixed/NS", ["service.name", "service.namespace"], {"g": "Count"},
            {"service.name": "a", "service.namespace": "n", "g": 2.5}),
        emf(t, "Fixed/NS", [], {"g": "Count"}, {"g": 4}),
    ])


def test_unit_translation(log_target, forwarder):
    # Adapted from transformer.rs test_unit_translation.
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    units = {"u_ms": "ms", "u_s": "s", "u_us": "us", "u_ns": "ns", "u_by": "By", "u_bit": "bit", "u_one": "1",
             "u_empty": "", "u_kb": "Kilobytes", "u_curly": "{req}", "u_kib": "KiBy", "u_min": "min"}
    assert f.protobuf(request(resource_metrics({"service.name": "svc"},
                                               [gauge(name, [number_point(1, t)], unit=unit)
                                                for name, unit in units.items()]))) == 200
    expected_units = {"u_ms": "Milliseconds", "u_s": "Seconds", "u_us": "Microseconds", "u_ns": "Count",
                      "u_by": "Bytes", "u_bit": "Bits", "u_one": "Count", "u_empty": "Count", "u_kb": "Kilobytes",
                      "u_curly": "{req}", "u_kib": "KiBy", "u_min": "min"}
    assert_documents(wait_events(group, stream, 1), [
        emf(t, "svc", ["service.name"], expected_units, {"service.name": "svc", **{n: 1 for n in units}}),
    ])


def test_histograms_become_statistic_sets(log_target, forwarder):
    # Adapted from transformer.rs test_add_histogram_metric / ..._with_missing_optional_fields.
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    assert f.grpc(request(resource_metrics(SHOP, [
        histogram("lat", [histogram_point(t, 4, 10.0, 1.0, 4.0, bounds=[2.0], buckets=[1, 3])], unit="ms"),
        histogram("bare", [histogram_point(t, 2, 3.0)], temporality=CUMULATIVE),
        histogram("lat", [histogram_point(t, 6, 12.5, 0.5, 9.0, {"op": "get"}, bounds=[2.0], buckets=[2, 4])],
                  unit="ms"),
        gauge("lat_gauge", [number_point(5, t)], unit="ms"),
    ]))) is not None
    assert_documents(wait_events(group, stream, 3), [
        emf(t, "shop/checkout", SHOP_DIMS, {"lat": "Milliseconds", "bare": "Count"},
            {**SHOP_FIELDS, "lat": {"Count": 4, "Sum": 10.0, "Min": 1.0, "Max": 4.0},
             "bare": {"Count": 2, "Sum": 3.0, "Min": 0.0, "Max": 0.0}}),
        emf(t, "shop/checkout", SHOP_DIMS + ["op"], {"lat": "Milliseconds"},
            {**SHOP_FIELDS, "op": "get", "lat": {"Count": 6, "Sum": 12.5, "Min": 0.5, "Max": 9.0}}),
        emf(t, "shop/checkout", SHOP_DIMS, {"lat_gauge": "Milliseconds"}, {**SHOP_FIELDS, "lat_gauge": 5}),
    ])


def test_duplicate_metric_names_in_one_document(log_target, forwarder):
    # Adapted from transformer.rs test_duplicate_metric_name_in_same_group.
    group, stream = log_target
    f = forwarder(group, stream)
    t = now_ms() - 3000
    assert f.protobuf(request(resource_metrics({"service.name": "svc"}, [
        gauge("hits", [number_point(3, t)], unit="1"),
        gauge("hits", [number_point(9, t)], unit="s"),
        histogram("lat", [histogram_point(t, 1, 2.0, 2.0, 2.0)], unit="ms"),
        histogram("lat", [histogram_point(t, 7, 70.0, 5.0, 15.0)], unit="s"),
    ]))) == 200
    assert_documents(wait_events(group, stream, 2), [
        emf(t, "svc", ["service.name"], {"hits": "Count"}, {"service.name": "svc", "hits": 3}),
        emf(t, "svc", ["service.name"], {"lat": "Seconds"},
            {"service.name": "svc", "lat": {"Count": 7, "Sum": 70.0, "Min": 5.0, "Max": 15.0}}),
    ])
