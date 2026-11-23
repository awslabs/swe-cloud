"""Comparison of EMF log events under the public equivalence policy.

* JSON key order is ignored; numbers compare by value (1 == 1.0 == 1e0) but never equal booleans.
* Metrics[] entries and the members of a dimension set compare as multisets.
* Log events compare as a multiset; each event's timestamp must equal its `_aws.Timestamp`.
"""

import json
from collections import Counter


def canonical(value):
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    if isinstance(value, str):
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, list):
        return ("list", tuple(canonical(v) for v in value))
    if isinstance(value, dict):
        return ("obj", tuple(sorted((k, canonical(v)) for k, v in value.items())))
    raise TypeError(type(value))


def _unordered(items):
    return ("bag", tuple(sorted((canonical(i) for i in items), key=repr)))


def document_key(document):
    """Canonical form of one EMF document; unordered collections become sorted bags."""
    assert isinstance(document, dict), f"EMF document is not a JSON object: {document!r}"
    key = []
    for name, value in document.items():
        if name == "_aws" and isinstance(value, dict):
            meta = []
            for meta_name, meta_value in value.items():
                if meta_name == "CloudWatchMetrics" and isinstance(meta_value, list):
                    directives = []
                    for directive in meta_value:
                        if not isinstance(directive, dict):
                            directives.append(canonical(directive))
                            continue
                        parts = []
                        for part, content in directive.items():
                            if part == "Metrics" and isinstance(content, list):
                                parts.append((part, _unordered(content)))
                            elif part == "Dimensions" and isinstance(content, list):
                                sets = [_unordered(s) if isinstance(s, list) else canonical(s) for s in content]
                                parts.append((part, ("bag", tuple(sorted(sets, key=repr)))))
                            else:
                                parts.append((part, canonical(content)))
                        directives.append(("obj", tuple(sorted(parts))))
                    meta.append((meta_name, ("list", tuple(directives))))
                else:
                    meta.append((meta_name, canonical(meta_value)))
            key.append((name, ("obj", tuple(sorted(meta)))))
        else:
            key.append((name, canonical(value)))
    return tuple(sorted(key))


def emf(timestamp, namespace, dimensions, metrics, fields):
    """Expected document: `metrics` maps metric name to its CloudWatch unit."""
    return {
        "_aws": {
            "Timestamp": timestamp,
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [list(dimensions)],
                "Metrics": [{"Name": n, "Unit": u, "StorageResolution": 60} for n, u in metrics.items()],
            }],
        },
        **fields,
    }


def parse_events(events):
    documents = []
    for event in events:
        try:
            document = json.loads(event["message"])
        except ValueError as error:
            raise AssertionError(f"log event message is not JSON: {event['message'][:500]!r}") from error
        assert isinstance(document, dict), f"log event message is not a JSON object: {event['message'][:500]!r}"
        stamp = document.get("_aws", {}).get("Timestamp") if isinstance(document.get("_aws"), dict) else None
        assert type(stamp) in (int, float) and stamp == event["timestamp"], (
            f"log event timestamp {event['timestamp']} differs from _aws.Timestamp {stamp!r}")
        documents.append(document)
    return documents


def assert_documents(events, expected):
    observed = parse_events(events)
    got = Counter(document_key(d) for d in observed)
    want = Counter(document_key(d) for d in expected)
    if got != want:
        def show(docs):
            return "\n".join("  " + json.dumps(d, sort_keys=True) for d in docs)
        missing = [d for d in expected if want[document_key(d)] > got.get(document_key(d), 0)]
        extra = [d for d in observed if got[document_key(d)] > want.get(document_key(d), 0)]
        raise AssertionError(f"EMF documents differ.\nmissing:\n{show(missing)}\nunexpected:\n{show(extra)}\n"
                             f"observed ({len(observed)}):\n{show(observed)}")
