"""Document ids taken from fields: nested paths, composite ids, value spellings and rejections."""

import pytest

from support import BULK_FAILED, assert_documents, documents, exists, expected_sources, final, load, messages, ndjson, sources, worker_errors, write_lines


def test_nested_and_composite_ids_across_workers(server, index, root):
    lines = ndjson(*({"a": {"b": n}, "c": c, "v": v} for n, c, v in [(1, "x", "one"), (2, "y", "two"), (3, "z", "three"), (4, "w", "four"), (5, "q", "five")]))
    lines.insert(2, "")
    path = write_lines(root, "docs.ndjson", lines)
    result = load(server, index, "-id", "a.b,c", "-size", "2", "-w", "3", path=path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert_documents(
        server,
        index,
        {
            "1x": {"a": {"b": 1}, "c": "x", "v": "one"},
            "2y": {"a": {"b": 2}, "c": "y", "v": "two"},
            "3z": {"a": {"b": 3}, "c": "z", "v": "three"},
            "4w": {"a": {"b": 4}, "c": "w", "v": "four"},
            "5q": {"a": {"b": 5}, "c": "q", "v": "five"},
        },
    )


@pytest.mark.parametrize(("spec", "expected"), [("a,b", "xy"), ("a b", "xy"), (" a , b ", "xy"), ("a,,b", "xy"), ("b a", "yx"), ("a,a", "xx"), ("b", "y")])
def test_id_field_list_syntax(server, index, root, spec, expected):
    path = write_lines(root, "in.ndjson", ['{"a":"x","b":"y","c":"z"}'])
    result = load(server, index, "-id", spec, "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {expected: {"a": "x", "b": "y", "c": "z"}})


def test_numeric_ids_keep_their_spelling(server, index, root):
    lines = ['{"n":1.50}', '{"n":10}', '{"n":-3}', '{"n":1e3}', '{"n":12345678901234567890}', '{"n":0.1E-2}']
    path = write_lines(root, "num.ndjson", lines)
    result = load(server, index, "-id", "n", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(
        server,
        index,
        {"1.50": {"n": 1.5}, "10": {"n": 10}, "-3": {"n": -3}, "1e3": {"n": 1000}, "12345678901234567890": {"n": 12345678901234567890}, "0.1E-2": {"n": 0.001}},
    )


def test_deep_paths_mixed_with_top_level_fields(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"a":{"b":{"c":"deep"}}, "z": 1}', '{"a":{"b":{"c":7}}, "z": 2.0}'])
    result = load(server, index, "-id", "a.b.c z", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"deep1": {"a": {"b": {"c": "deep"}}, "z": 1}, "72.0": {"a": {"b": {"c": 7}}, "z": 2}})


def test_dotted_path_reads_nested_object_not_dotted_key(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"a.b":"lit","a":{"b":"nest"}}'])
    result = load(server, index, "-id", "a.b", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"nest": {"a.b": "lit", "a": {"b": "nest"}}})


def test_quotes_and_unicode_in_ids(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"id": 'x y"q', "v": 1}, {"id": "é/ü", "v": 2}))
    result = load(server, index, "-id", "id", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {'x y"q': {"id": 'x y"q', "v": 1}, "é/ü": {"id": "é/ü", "v": 2}})


def test_underscore_id_field_is_removed_from_source(server, new_index, root):
    plain = new_index()
    path = write_lines(root, "u.ndjson", ['{"_id":"u1","v":1,"w":{"x":2.50,"t":true}}', '{"_id":"u2","v":2,"s":null}'])
    result = load(server, plain, "-id", "_id", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, plain, {"u1": {"v": 1, "w": {"x": 2.5, "t": True}}, "u2": {"v": 2, "s": None}})

    composite = new_index()
    path = write_lines(root, "c.ndjson", ['{"_id":"u1","k":"s","v":1}'])
    result = load(server, composite, "-id", "_id,k", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, composite, {"u1s": {"k": "s", "v": 1}})


def test_underscore_id_without_id_flag_is_rejected(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"_id":"u1","v":1}', '{"v":2}'])
    result = load(server, index, "-w", "1", "-size", "1", path=path)
    assert result.returncode == 1
    assert final(result.stderr) == worker_errors(BULK_FAILED)
    assert sources(server, index) == expected_sources({"v": 2})


def test_unusable_id_values_drop_their_batch(server, index, root):
    lines = ['{"id":"c1"}', '{"id":"c2"}', '{"id":true}', '{"id":"c4"}', '{"id":"c5"}', '{"x":"c6"}', '{"id":"c7"}', '{"id":null}', '{"id":"c9"}', '{"id":{"o":1}}', '{"id":"c11"}', '{"id":"c12"}', '{"id":[1]}']
    path = write_lines(root, "bad.ndjson", lines)
    result = load(server, index, "-id", "id", "-w", "1", "-size", "2", "-r", "4s", path=path)
    assert result.returncode == 1
    assert result.stdout == ""
    convert = "cannot convert id value to string"
    assert messages(result.stderr) == [worker_errors(convert, 'document has no ID field (id): {"x":"c6"}', convert, convert, convert)]
    assert_documents(server, index, {"c1": {"id": "c1"}, "c2": {"id": "c2"}, "c11": {"id": "c11"}, "c12": {"id": "c12"}})


@pytest.mark.parametrize("line", ['{"a":{"b":null},"v":1}', '{"a":"str","v":1}', '{"a":{"c":1},"v":1}', '{"v":1}'])
def test_unresolvable_nested_path(server, index, root, line):
    path = write_lines(root, "in.ndjson", [line, '{"a":{"b":"ok"},"v":2}'])
    result = load(server, index, "-id", "a.b", "-w", "1", path=path)
    assert result.returncode == 1
    assert messages(result.stderr) == [worker_errors(f"document has no ID field (a.b): {line}")]
    assert exists(server, index)
    assert documents(server, index) == {}


def test_second_field_missing_names_that_field(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"a":"x","v":1}'])
    result = load(server, index, "-id", "a,b.c", "-w", "1", path=path)
    assert result.returncode == 1
    assert messages(result.stderr) == [worker_errors('document has no ID field (b.c): {"a":"x","v":1}')]


def test_non_object_lines_with_id(server, index, root):
    path = write_lines(root, "in.ndjson", ["[1,2]", '"str"', '{"a":"ok"}'])
    result = load(server, index, "-id", "a", "-w", "1", "-size", "1", path=path)
    assert result.returncode == 1
    final_message = final(result.stderr)
    assert final_message.startswith("worker errors occurred: ")
    parts = final_message.removeprefix("worker errors occurred: ").split("; ")
    assert len(parts) == 2
    assert all(p.startswith(WORKER_DECODE) for p in parts), parts
    assert_documents(server, index, {"ok": {"a": "ok"}})


WORKER_DECODE = "worker worker-0: worker bulk index operation failed: failed to json decode doc: "


def test_empty_id_is_refused_by_server_but_batch_partly_loads(server, index, root):
    lines = ['{"id":"","v":1}', '{"id":"ok","v":2}']
    path = write_lines(root, "in.ndjson", lines)
    result = load(server, index, "-id", "id", "-w", "1", path=path)
    assert result.returncode == 1
    assert final(result.stderr) == worker_errors(BULK_FAILED)
    assert set(lines) <= set(messages(result.stderr))
    assert_documents(server, index, {"ok": {"id": "ok", "v": 2}})


def test_many_documents_default_workers(server, index, root):
    docs = [{"k": {"p": f"g{i % 7}", "n": i}, "s": f"-{i}"} for i in range(300)]
    path = write_lines(root, "many.ndjson", ndjson(*docs))
    result = load(server, index, "-size", "7", "-id", "k.p,k.n,s", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {f"g{i % 7}{i}-{i}": d for i, d in enumerate(docs)})


def test_ten_thousand_documents(server, new_index, root):
    # Adapted from the upstream minimal-configuration test (fixtures/v10k.jsonl).
    path = write_lines(root, "v10k.jsonl", ndjson(*({"v": str(i)} for i in range(10000))))
    single = new_index()
    result = load(server, single, "-size", "5000", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    observed = documents(server, single)
    assert sorted(s["v"] for s in observed.values()) == sorted(str(i) for i in range(10000))

    keyed = new_index()
    result = load(server, keyed, "-id", "v", "-size", "1000", "-w", "4", path=path)
    assert result.returncode == 0, result.stderr
    observed = documents(server, keyed)
    assert sorted(observed) == sorted(str(i) for i in range(10000))
    assert all(list(s) == ["v"] and s["v"] == k for k, s in observed.items())
