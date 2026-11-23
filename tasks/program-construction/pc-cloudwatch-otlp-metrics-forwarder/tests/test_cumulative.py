"""Multi-export behavior: cumulative-to-delta conversion, the retain flag, resets, summaries and process state."""

import time

from emf_check import assert_documents, emf
from emf_driver import (CUMULATIVE, DELTA, gauge, histogram, histogram_point, now_ms, number_point, request,
                        resource_metrics, sum_metric, summary, summary_point, wait_events)

SVC = {"service.name": "svc"}
GAP = 1.5  # well beyond the 200 ms batch window, so exports are transformed separately


def seen(t):
    return gauge("seen", [number_point(1, t)])


def seen_doc(t):
    return emf(t, "svc", ["service.name"], {"seen": "Count"}, {"service.name": "svc", "seen": 1})


def test_cumulative_sums_report_change_between_exports(log_target, forwarder):
    # Adapted from transformer.rs test_delta_calculation_for_cumulative_sum / test_delta_calculator_metric_reset.
    group, stream = log_target
    f = forwarder(group, stream)
    t1 = now_ms() - 6000
    f.protobuf(request(resource_metrics(SVC, [seen(t1), sum_metric("total", [number_point(10, t1)]),
                                              sum_metric("ratio", [number_point(1.5, t1)], unit="s"),
                                              sum_metric("level", [number_point(5.0, t1)], monotonic=False)])))
    time.sleep(GAP)
    t2 = now_ms() - 4000
    f.protobuf(request(resource_metrics(SVC, [sum_metric("total", [number_point(25, t2)]),
                                              sum_metric("ratio", [number_point(4.0, t2)], unit="s"),
                                              sum_metric("level", [number_point(2.0, t2)], monotonic=False)])))
    time.sleep(GAP)
    t3 = now_ms() - 2000
    f.protobuf(request(resource_metrics(SVC, [sum_metric("total", [number_point(20, t3)]),
                                              sum_metric("ratio", [number_point(4.0, t3)], unit="s"),
                                              sum_metric("level", [number_point(3.5, t3)], monotonic=False)])))
    sums = {"total": "Count", "ratio": "Seconds", "level": "Count"}
    assert_documents(wait_events(group, stream, 3), [
        seen_doc(t1),
        # 25-10, 4.0-1.5, and a decrease (5.0 -> 2.0) reports the current value.
        emf(t2, "svc", ["service.name"], sums, {"service.name": "svc", "total": 15, "ratio": 2.5, "level": 2.0}),
        # A decrease (25 -> 20) reports the current value; an unchanged value reports zero.
        emf(t3, "svc", ["service.name"], sums, {"service.name": "svc", "total": 20, "ratio": 0.0, "level": 1.5}),
    ])


def test_delta_temporality_sums_pass_through(log_target, forwarder):
    # Adapted from transformer.rs test_non_cumulative_sum_no_delta.
    group, stream = log_target
    f = forwarder(group, stream)
    t1 = now_ms() - 4000
    f.protobuf(request(resource_metrics(SVC, [sum_metric("d", [number_point(4, t1)], temporality=DELTA),
                                              sum_metric("dd", [number_point(0.5, t1)], temporality=DELTA)])))
    time.sleep(GAP)
    t2 = now_ms() - 2000
    f.protobuf(request(resource_metrics(SVC, [sum_metric("d", [number_point(3, t2)], temporality=DELTA),
                                              sum_metric("dd", [number_point(0.25, t2)], temporality=DELTA)])))
    assert_documents(wait_events(group, stream, 2), [
        emf(t1, "svc", ["service.name"], {"d": "Count", "dd": "Count"}, {"service.name": "svc", "d": 4, "dd": 0.5}),
        emf(t2, "svc", ["service.name"], {"d": "Count", "dd": "Count"}, {"service.name": "svc", "d": 3, "dd": 0.25}),
    ])


def test_each_attribute_set_is_a_separate_series(log_target, forwarder):
    # `pod` is excluded from dimensions, yet still distinguishes series and documents.
    group, stream = log_target
    f = forwarder(group, stream, "--awsemf-exporter-exclude-dimensions", "pod")

    def export(t, p1, p2, g):
        f.protobuf(request(resource_metrics(SVC, [
            sum_metric("c", [number_point(p1, t, {"pod": "p1"}), number_point(p2, t, {"pod": "p2"})]),
            gauge("c", [number_point(g, t, {"pod": "p1"})])])))

    t1 = now_ms() - 4000
    export(t1, 10, 100, 1)
    time.sleep(GAP)
    t2 = now_ms() - 2000
    export(t2, 13, 150, 2)
    doc = lambda t, pod, value: emf(t, "svc", ["service.name"], {"c": "Count"},  # noqa: E731
                                    {"service.name": "svc", "pod": pod, "c": value})
    assert_documents(wait_events(group, stream, 4), [
        doc(t1, "p1", 1), doc(t2, "p1", 2), doc(t2, "p1", 3.0), doc(t2, "p2", 50.0),
    ])


def test_summaries_report_count_and_sum_changes(log_target, forwarder):
    # Adapted from transformer.rs test_add_summary_metric / test_summary_delta_calculation.
    group, stream = log_target
    f = forwarder(group, stream)
    t1 = now_ms() - 6000
    f.protobuf(request(resource_metrics(SVC, [seen(t1), summary("rpc", [summary_point(t1, 5, 12.5, [(0.5, 2.0),
                                                                                                   (0.99, 4.0)])],
                                                                  unit="ms")])))
    time.sleep(GAP)
    t2 = now_ms() - 4000
    f.protobuf(request(resource_metrics(SVC, [summary("rpc", [summary_point(t2, 8, 20.0, [(0.5, 9.0)])], unit="ms")])))
    time.sleep(GAP)
    t3 = now_ms() - 2000
    f.protobuf(request(resource_metrics(SVC, [summary("rpc", [summary_point(t3, 9, 18.0)], unit="ms")])))
    rpc = {"rpc": "Milliseconds"}
    assert_documents(wait_events(group, stream, 3), [
        seen_doc(t1),
        emf(t2, "svc", ["service.name"], rpc, {"service.name": "svc", "rpc_count": 3, "rpc_sum": 7.5}),
        emf(t3, "svc", ["service.name"], rpc, {"service.name": "svc", "rpc_count": 1, "rpc_sum": -2.0}),
    ])


def test_retain_initial_value_flag(log_target, forwarder):
    # Adapted from transformer.rs test_delta_calculation_with_retain_initial_value.
    group, stream = log_target
    f = forwarder(group, stream, "--awsemf-exporter-retain-initial-value-of-delta-metric")
    t1 = now_ms() - 4000
    f.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(7, t1)]),
                                              summary("q", [summary_point(t1, 3, 9.0)]),
                                              sum_metric("d", [number_point(2, t1)], temporality=DELTA)])))
    time.sleep(GAP)
    t2 = now_ms() - 2000
    f.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(12, t2)]),
                                              summary("q", [summary_point(t2, 5, 10.0)]),
                                              sum_metric("d", [number_point(2, t2)], temporality=DELTA)])))
    one = ["service.name"]
    assert_documents(wait_events(group, stream, 4), [
        emf(t1, "svc", one, {"c": "Count", "d": "Count"}, {"service.name": "svc", "c": 7, "d": 2}),
        emf(t1, "svc", one, {"q": "Count"}, {"service.name": "svc", "q_count": 3, "q_sum": 9.0}),
        emf(t2, "svc", one, {"c": "Count", "d": "Count"}, {"service.name": "svc", "c": 5, "d": 2}),
        emf(t2, "svc", one, {"q": "Count"}, {"service.name": "svc", "q_count": 2, "q_sum": 1.0}),
    ])


def test_cumulative_histograms_are_not_differenced(log_target, forwarder):
    group, stream = log_target
    f = forwarder(group, stream)
    t1 = now_ms() - 4000
    f.json(request(resource_metrics(SVC, [histogram("h", [histogram_point(t1, 3, 6.0, 1.0, 3.0)], unit="s",
                                                    temporality=CUMULATIVE)])))
    time.sleep(GAP)
    t2 = now_ms() - 2000
    f.json(request(resource_metrics(SVC, [histogram("h", [histogram_point(t2, 5, 9.0, 1.0, 3.5)], unit="s",
                                                    temporality=CUMULATIVE)])))
    assert_documents(wait_events(group, stream, 2), [
        emf(t1, "svc", ["service.name"], {"h": "Seconds"},
            {"service.name": "svc", "h": {"Count": 3, "Sum": 6.0, "Min": 1.0, "Max": 3.0}}),
        emf(t2, "svc", ["service.name"], {"h": "Seconds"},
            {"service.name": "svc", "h": {"Count": 5, "Sum": 9.0, "Min": 1.0, "Max": 3.5}}),
    ])


def test_series_state_does_not_survive_restart(log_target, forwarder):
    group, stream = log_target
    first = forwarder(group, stream)
    t1 = now_ms() - 4000
    first.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(10, t1)]), seen(t1)])))
    assert_documents(wait_events(group, stream, 1), [seen_doc(t1)])
    assert first.stop() == 0
    second = forwarder(group, stream)
    t2 = now_ms() - 2000
    second.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(25, t2)]), seen(t2)])))
    time.sleep(GAP)
    t3 = now_ms() - 1000
    second.protobuf(request(resource_metrics(SVC, [sum_metric("c", [number_point(26, t3)])])))
    assert_documents(wait_events(group, stream, 3), [
        seen_doc(t1), seen_doc(t2),
        emf(t3, "svc", ["service.name"], {"c": "Count"}, {"service.name": "svc", "c": 1}),
    ])
