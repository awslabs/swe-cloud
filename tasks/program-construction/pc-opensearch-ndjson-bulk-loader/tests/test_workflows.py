"""Sequences of runs against one index: purge, appends, conflicts and mixed actions."""

import time

from support import (
    BULK_FAILED,
    assert_documents,
    documents,
    expected_sources,
    final,
    index_settings,
    load,
    messages,
    ndjson,
    properties,
    sources,
    worker_errors,
    write_lines,
)


def test_purge_recreates_index_with_new_mapping(server, index, root):
    first = write_lines(root, "first.ndjson", ndjson({"id": "k1", "b": "text"}, {"id": "k9", "b": "old"}))
    result = load(server, index, "-w", "1", "-id", "id", "-mapping", '{"properties":{"b":{"type":"keyword"}}}', path=first)
    assert result.returncode == 0, result.stderr
    assert properties(server, index)["b"] == {"type": "keyword"}

    # Without purging, a long mapping for b conflicts with the existing keyword field.
    second = write_lines(root, "second.ndjson", ndjson({"id": "k1", "b": 5}, {"id": "k2", "b": 6}, {"id": "k1", "b": 7}))
    result = load(server, index, "-w", "1", "-id", "id", "-mapping", '{"properties":{"b":{"type":"long"}}}', path=second)
    assert result.returncode == 1
    assert final(result.stderr).startswith("failed to apply mapping with 400 Bad Request: ")
    assert_documents(server, index, {"k1": {"id": "k1", "b": "text"}, "k9": {"id": "k9", "b": "old"}})

    result = load(server, index, "-w", "1", "-id", "id", "-purge", "-purge-pause", "0s", "-mapping", '{"properties":{"b":{"type":"long"}}}', path=second)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"k1": {"id": "k1", "b": 7}, "k2": {"id": "k2", "b": 6}})
    assert properties(server, index)["b"] == {"type": "long"}


def test_purge_from_stdin_with_mapping_file(server, index, root):
    # The preparation probe's purge scenario: mapping file, stdin input, last write wins.
    load(server, index, "-w", "1", stdin='{"id":"gone","name":"x","n":0}\n')
    write_lines(root, "mapping.json", ['{"properties": {"name": {"type": "keyword"}, "n": {"type": "integer"}}}'])
    stdin = "\n".join(ndjson({"id": "k1", "name": "alpha", "n": 1}, {"id": "k2", "name": "beta", "n": 2}, {"id": "k1", "name": "gamma", "n": 3})) + "\n"
    result = load(server, index, "-purge", "-purge-pause", "1s", "-mapping", "mapping.json", "-id", "id", "-w", "1", "-size", "10", stdin=stdin, cwd=root)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"k1": {"id": "k1", "name": "gamma", "n": 3}, "k2": {"id": "k2", "name": "beta", "n": 2}})
    props = properties(server, index)
    assert props["name"] == {"type": "keyword"} and props["n"] == {"type": "integer"}


def test_purge_applies_creation_config_again(server, index, root):
    write_lines(root, "create.json", ['{"settings":{"number_of_shards":2}}'])
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, index, "-w", "1", "-c", '{"settings":{"number_of_shards":3,"number_of_replicas":0}}', path=path)
    assert result.returncode == 0, result.stderr

    # An existing index keeps its configuration; -c only applies on creation.
    result = load(server, index, "-w", "1", "-c", "create.json", path=path)
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["number_of_shards"], settings["number_of_replicas"]) == ("3", "0")
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 1})

    result = load(server, index, "-w", "1", "-purge", "-purge-pause", "0s", "-c", "create.json", path=path)
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["number_of_shards"], settings["number_of_replicas"], settings["refresh_interval"]) == ("2", "1", "1s")
    assert sources(server, index) == expected_sources({"v": 1})


def test_purge_of_missing_index_pauses(server, index):
    started = time.monotonic()
    result = load(server, index, "-w", "1", "-purge", "-purge-pause", "2500ms", stdin='{"v":1}\n')
    assert time.monotonic() - started >= 2.5
    assert result.returncode == 0, result.stderr
    assert sources(server, index) == expected_sources({"v": 1})


def test_later_load_with_type_conflict(server, index, root):
    # Adapted from the upstream extra/ok.jsonl and extra/broken.jsonl fixtures.
    ok_lines = ['{"a": "hello", "b": 123}', '{"a": "world", "b": 234}']
    result = load(server, index, "-w", "1", path=write_lines(root, "ok.jsonl", ok_lines))
    assert result.returncode == 0, result.stderr
    assert properties(server, index)["b"] == {"type": "long"}

    broken = ['{"a": "hello", "b": "x123"}', '{"a": "again", "b": 345}']
    result = load(server, index, "-w", "1", path=write_lines(root, "broken.jsonl", broken))
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert logged[-1] == worker_errors(BULK_FAILED)
    assert set(broken) <= set(logged) and not set(ok_lines) & set(logged)
    assert sources(server, index) == expected_sources({"a": "hello", "b": 123}, {"a": "world", "b": 234}, {"a": "again", "b": 345})


def test_create_index_update_sequence(server, index, root):
    def run(*flags, docs):
        path = write_lines(root, "step.ndjson", ndjson(*docs))
        return load(server, index, "-w", "1", "-id", "id", *flags, path=path)

    result = run("-optype", "create", docs=[{"id": "a", "v": 1, "x": "a"}, {"id": "b", "v": 1}])
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"a": {"id": "a", "v": 1, "x": "a"}, "b": {"id": "b", "v": 1}})

    result = run("-optype", "create", docs=[{"id": "a", "v": 2}, {"id": "c", "v": 2}])
    assert result.returncode == 1
    assert_documents(server, index, {"a": {"id": "a", "v": 1, "x": "a"}, "b": {"id": "b", "v": 1}, "c": {"id": "c", "v": 2}})

    result = run(docs=[{"id": "a", "v": 3}])
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"a": {"id": "a", "v": 3}, "b": {"id": "b", "v": 1}, "c": {"id": "c", "v": 2}})

    result = run("-optype", "update", docs=[{"id": "b", "w": True}, {"id": "d", "v": 4}])
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"a": {"id": "a", "v": 3}, "b": {"id": "b", "v": 1, "w": True}, "c": {"id": "c", "v": 2}, "d": {"id": "d", "v": 4}})
    assert index_settings(server, index)["refresh_interval"] == "1s"


def test_failed_run_keeps_earlier_documents(server, index, root):
    result = load(server, index, "-w", "1", "-id", "id", stdin='{"id":"keep","v":1}\n')
    assert result.returncode == 0, result.stderr
    path = write_lines(root, "bad.ndjson", ['{"id":"new","v":2}', '{"v":3}'])
    result = load(server, index, "-w", "1", "-id", "id", "-r", "8s", path=path)
    assert result.returncode == 1
    assert messages(result.stderr) == [worker_errors('document has no ID field (id): {"v":3}')]
    assert_documents(server, index, {"keep": {"id": "keep", "v": 1}})
    assert index_settings(server, index)["refresh_interval"] == "8s"
