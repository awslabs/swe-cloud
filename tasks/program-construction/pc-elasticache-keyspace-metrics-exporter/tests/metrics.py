"""Exposition parsing and comparison helpers for the keyspace-gauges suite.

Samples are compared as (sample name, complete label set, value). Values are compared
numerically (the parser yields floats, so `1`, `1.0` and `1e0` are equal); NaN equals NaN.
Sample order, HELP/TYPE lines and number spelling are not graded.
"""

import math
from collections import Counter
import urllib.error
import urllib.request

from prometheus_client.parser import text_string_to_metric_families

# In-scope families (without the namespace prefix) whose values are graded exactly.
GRADED = {
    "up",
    "key_size",
    "key_value",
    "key_value_as_string",
    "keys_count",
    "key_group_count",
    "number_of_distinct_key_groups",
    "stream_length",
    "stream_radix_tree_keys",
    "stream_radix_tree_nodes",
    "stream_last_generated_id",
    "stream_groups",
    "stream_max_deleted_entry_id",
    "stream_first_entry_id",
    "stream_last_entry_id",
    "stream_entries_added_total",
    "stream_group_consumers",
    "stream_group_messages_pending",
    "stream_group_last_delivered_id",
    "stream_group_entries_read",
    "stream_group_lag",
    "stream_group_consumer_messages_pending",
    "script_values",
    "script_result",
}
GROUP_MEMORY = "key_group_memory_usage_bytes"  # presence and positivity only


def http_get(base, path, timeout=30):
    """Return (status, body) without raising on HTTP error statuses."""
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def parse(text, namespace="redis"):
    """All samples as (family-suffix, labels, value) for metric names under the namespace."""
    prefix = namespace + "_"
    out = []
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name.startswith(prefix):
                out.append((sample.name[len(prefix):], dict(sample.labels), float(sample.value)))
    return out


def select(samples, keep, names=GRADED):
    return [s for s in samples if s[0] in names and keep(s)]


def mentions(prefix):
    """Samples with a label value containing the test's unique key prefix."""
    return lambda s: any(prefix in v for v in s[1].values())


def _key(sample):
    name, labels, value = sample
    return (name, tuple(sorted(labels.items())), "nan" if math.isnan(value) else value)


def assert_samples(actual, expected):
    """Exact multiset equality of samples; reports missing and unexpected entries."""
    got = Counter(_key(s) for s in actual)
    want = Counter(_key((n, l, float(v))) for n, l, v in expected)
    missing, extra = sorted(want - got, key=repr), sorted(got - want, key=repr)
    assert not missing and not extra, f"missing: {missing}\nunexpected: {extra}"


def value_of(samples, name, labels=None):
    found = [s[2] for s in samples if s[0] == name and s[1] == (labels or {})]
    assert len(found) == 1, f"expected exactly one {name}{labels or {}}, found {found}"
    return found[0]


# Expected-row builders for stream families.
def stream(db, key, *, length, keys, nodes, last_generated, groups, max_deleted, first, last, added):
    labels = {"db": db, "stream": key}
    values = {
        "stream_length": length, "stream_radix_tree_keys": keys, "stream_radix_tree_nodes": nodes,
        "stream_last_generated_id": last_generated, "stream_groups": groups,
        "stream_max_deleted_entry_id": max_deleted, "stream_first_entry_id": first,
        "stream_last_entry_id": last, "stream_entries_added_total": added,
    }
    return [(name, labels, value) for name, value in values.items()]


def group(db, key, name, *, consumers, pending, last_delivered, entries_read, lag):
    labels = {"db": db, "stream": key, "group": name}
    values = {
        "stream_group_consumers": consumers, "stream_group_messages_pending": pending,
        "stream_group_last_delivered_id": last_delivered, "stream_group_entries_read": entries_read,
        "stream_group_lag": lag,
    }
    return [(metric, labels, value) for metric, value in values.items()]


def consumer(db, key, group_name, name, pending):
    labels = {"db": db, "stream": key, "group": group_name, "consumer": name}
    return [("stream_group_consumer_messages_pending", labels, pending)]
