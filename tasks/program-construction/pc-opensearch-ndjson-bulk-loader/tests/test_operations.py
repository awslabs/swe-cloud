"""Single runs: bulk actions, broken input, batching, mappings, creation config and pipelines."""

import pytest

from support import (
    BULK_FAILED,
    assert_documents,
    documents,
    exists,
    expected_sources,
    final,
    http,
    index_settings,
    load,
    messages,
    ndjson,
    ok,
    properties,
    sources,
    worker_errors,
    write_lines,
)


def seed(server, index, docs):
    """Index documents directly, bypassing the program."""
    for key, source in docs.items():
        ok(server, "PUT", f"/{index}/_doc/{key}", source)
    ok(server, "POST", f"/{index}/_refresh")


def test_create_keeps_existing_and_loads_rest_of_batch(server, index, root):
    seed(server, index, {"1x": {"v": "one"}})
    lines = ['{"k": "1x", "v": "changed"}', '{"k": "9n", "v": "nine"}']
    path = write_lines(root, "dup.ndjson", lines)
    result = load(server, index, "-id", "k", "-optype", "create", "-w", "1", "-r", "5s", path=path)
    assert result.returncode == 1
    assert result.stdout == ""
    logged = messages(result.stderr)
    assert logged[-1] == worker_errors(BULK_FAILED)
    assert any(line.startswith("request body: ") for line in logged)
    assert set(lines) <= set(logged)
    assert_documents(server, index, {"1x": {"v": "one"}, "9n": {"k": "9n", "v": "nine"}})
    assert index_settings(server, index)["refresh_interval"] == "5s"


def test_create_reports_each_failed_batch(server, index, root):
    seed(server, index, {"m1": {"id": "m1", "v": 1}, "m2": {"id": "m2", "v": 1}})
    lines = ndjson(*({"id": k, "v": 2} for k in ("m1", "m3", "m4", "m2", "m5", "m6")))
    path = write_lines(root, "in.ndjson", lines)
    result = load(server, index, "-id", "id", "-w", "1", "-size", "2", "-optype", "create", path=path)
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert logged[-1] == worker_errors(BULK_FAILED, BULK_FAILED)
    assert sum(line.startswith("request body: ") for line in logged) == 2
    assert set(lines[:4]) <= set(logged)
    assert not set(lines[4:]) & set(logged)
    assert_documents(server, index, {"m1": {"id": "m1", "v": 1}, "m2": {"id": "m2", "v": 1}, **{k: {"id": k, "v": 2} for k in ("m3", "m4", "m5", "m6")}})


def test_create_with_auto_ids(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}, {"v": 1}))
    result = load(server, index, "-optype", "create", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 1})


def test_index_replaces_whole_document(server, index, root):
    seed(server, index, {"k": {"id": "k", "old": True, "v": 0}})
    path = write_lines(root, "in.ndjson", ndjson({"id": "k", "v": 1}, {"id": "k", "v": 2}, {"id": "j", "v": 1}, {"id": "k", "v": 3}))
    result = load(server, index, "-id", "id", "-w", "1", "-size", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"k": {"id": "k", "v": 3}, "j": {"id": "j", "v": 1}})


def test_update_merges_into_existing_and_upserts(server, index, root):
    seed(server, index, {"f1": {"id": "f1", "a": 1, "b": 2, "o": {"x": 1, "arr": [1, 2]}, "keep": True}, "f2": {"id": "f2", "a": 5}})
    path = write_lines(root, "in.ndjson", ndjson({"id": "f1", "b": 3, "c": 4, "o": {"y": 2, "arr": [3]}}, {"id": "f3", "c": 9}))
    result = load(server, index, "-id", "id", "-w", "1", "-optype", "update", path=path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert_documents(
        server,
        index,
        {
            "f1": {"id": "f1", "a": 1, "b": 3, "c": 4, "o": {"x": 1, "y": 2, "arr": [3]}, "keep": True},
            "f2": {"id": "f2", "a": 5},
            "f3": {"id": "f3", "c": 9},
        },
    )


def test_update_with_underscore_id(server, index, root):
    seed(server, index, {"w1": {"v": 1, "x": "kept"}})
    path = write_lines(root, "in.ndjson", ['{"_id":"w1","v":2}', '{"_id":"w2","v":3}'])
    result = load(server, index, "-id", "_id", "-w", "1", "-optype", "update", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"w1": {"v": 2, "x": "kept"}, "w2": {"v": 3}})


def test_update_without_id_fails_whole_request(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"c": 1}, {"c": 2}))
    result = load(server, index, "-w", "1", "-optype", "update", path=path)
    assert result.returncode == 1
    message = final(result.stderr)
    assert message.startswith(worker_errors("indexing failed with 400 Bad Request: "))
    assert "id is missing" in message
    assert exists(server, index)
    assert documents(server, index) == {}


def test_unknown_optype_fails_whole_request(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"id": "a"}))
    result = load(server, index, "-id", "id", "-w", "1", "-optype", "bogus", path=path)
    assert result.returncode == 1
    message = final(result.stderr)
    assert message.startswith(worker_errors("indexing failed with 400 Bad Request: "))
    assert "[bogus]" in message
    assert documents(server, index) == {}


def test_broken_json_line_is_refused_individually(server, index, root):
    lines = ['{"v":1}', "not json", '{"v":3}', "[1,2]"]
    path = write_lines(root, "in.ndjson", lines)
    result = load(server, index, "-w", "1", path=path)
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert logged[-1] == worker_errors(BULK_FAILED)
    assert set(lines) <= set(logged)
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 3})


def test_skipbroken_skips_invalid_lines(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"v":1}', "not json", '{"v":3', '{"v":4}'])
    result = load(server, index, "-w", "1", "-skipbroken", path=path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 4})


def test_broken_line_with_id_drops_its_batch(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"id":"e1"}', '{"id": broken', '{"id":"e3"}', '{"id":"e4"}', '{"id":"e5"}'])
    result = load(server, index, "-id", "id", "-w", "1", "-size", "2", path=path)
    assert result.returncode == 1
    message = final(result.stderr)
    assert message.startswith(worker_errors("failed to json decode doc: "))
    assert ";" not in message
    assert_documents(server, index, {"e3": {"id": "e3"}, "e4": {"id": "e4"}, "e5": {"id": "e5"}})


@pytest.mark.parametrize("filler", ["", "   ", "garbage"])
def test_batches_count_only_queued_lines(server, index, root, filler):
    flags = ["-skipbroken"] if filler == "garbage" else []
    lines = ['{"id":"b1"}', filler, '{"id":"b2"}', filler, '{"id":"b3"}', '{"nope":1}', '{"id":"b5"}', '{"id":"b6"}', '{"id":"b7"}', '{"id":"b8"}']
    path = write_lines(root, "in.ndjson", lines)
    result = load(server, index, "-id", "id", "-w", "1", "-size", "3", *flags, path=path)
    assert result.returncode == 1
    assert messages(result.stderr) == [worker_errors('document has no ID field (id): {"nope":1}')]
    assert_documents(server, index, {k: {"id": k} for k in ("b1", "b2", "b3", "b7", "b8")})


def test_mapping_from_file_and_from_text(server, new_index, root):
    write_lines(root, "mapping.json", ['{"properties": {"name": {"type": "keyword"}, "n": {"type": "integer"}}}'])
    path = write_lines(root, "in.ndjson", ndjson({"id": "k1", "name": "alpha", "n": 1}))
    from_file = new_index()
    result = load(server, from_file, "-mapping", "mapping.json", "-id", "id", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    props = properties(server, from_file)
    assert props["name"] == {"type": "keyword"} and props["n"] == {"type": "integer"}
    assert props["id"]["type"] == "text"

    from_text = new_index()
    result = load(server, from_text, "-mapping", '{"properties":{"name":{"type":"date"}}}', "-w", "1", stdin='{"name":"2024-01-02"}\n')
    assert result.returncode == 0, result.stderr
    assert properties(server, from_text) == {"name": {"type": "date"}}


def test_mapping_rejected_before_indexing(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, index, "-w", "1", "-r", "9s", "-mapping", '{"properties":{"v":{"type":"nosuchtype"}}}', path=path)
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert len(logged) == 1
    assert logged[0].startswith("failed to apply mapping with 400 Bad Request: ")
    assert "nosuchtype" in logged[0]
    assert exists(server, index)
    assert documents(server, index) == {}
    settings = index_settings(server, index)
    assert "refresh_interval" not in settings
    assert settings["number_of_replicas"] == "1"


def test_mapping_path_that_does_not_exist_is_sent_as_text(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, index, "-w", "1", "-mapping", "nofile.json", path=path)
    assert result.returncode == 1
    assert final(result.stderr).startswith("failed to apply mapping with 500 Internal Server Error: ")
    assert documents(server, index) == {}


def test_creation_config_text(server, index, root):
    config = '{"settings":{"number_of_shards":3,"number_of_replicas":0},"mappings":{"properties":{"v":{"type":"keyword"}}}}'
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, index, "-w", "1", "-c", config, path=path)
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["number_of_shards"], settings["number_of_replicas"], settings["refresh_interval"]) == ("3", "0", "1s")
    assert properties(server, index) == {"v": {"type": "keyword"}}
    assert sources(server, index) == expected_sources({"v": 1})


def test_creation_config_file_and_mapping_combine(server, new_index, root):
    write_lines(root, "create.json", ['{"settings":{"number_of_shards":2},"mappings":{"properties":{"a":{"type":"keyword"}}}}'])
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    combined = new_index()
    result = load(server, combined, "-w", "1", "-c", "create.json", "-mapping", '{"properties":{"b":{"type":"integer"}}}', path=path)
    assert result.returncode == 0, result.stderr
    assert index_settings(server, combined)["number_of_shards"] == "2"
    assert properties(server, combined) == {"a": {"type": "keyword"}, "b": {"type": "integer"}, "v": {"type": "long"}}

    conflicting = new_index()
    result = load(server, conflicting, "-w", "1", "-c", "create.json", "-mapping", '{"properties":{"a":{"type":"integer"}}}', path=path)
    assert result.returncode == 1
    assert final(result.stderr).startswith("failed to apply mapping with 400 Bad Request: ")
    assert properties(server, conflicting) == {"a": {"type": "keyword"}}
    assert documents(server, conflicting) == {}


def test_creation_config_rejected(server, index, root):
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, index, "-w", "1", "-c", '{"settings":{"bogus_x":1}}', path=path)
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert len(logged) == 2, logged
    assert logged[0].startswith("elasticsearch response was: ")
    assert "unknown setting [index.bogus_x]" in logged[0]
    assert logged[1] == ""
    assert not exists(server, index)


def test_invalid_index_name(server, root):
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, "BadName", "-w", "1", path=path)
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert len(logged) == 2, logged
    assert logged[0].startswith("elasticsearch response was: ")
    assert "Invalid index name [BadName], must be lowercase" in logged[0]
    assert logged[1] == ""
    assert not exists(server, "badname")


def test_pipeline_preprocesses_documents(server, index, root, pipeline):
    name = pipeline([{"set": {"field": "tag", "value": "piped"}}, {"remove": {"field": "drop"}}])
    path = write_lines(root, "in.ndjson", ndjson({"id": "p1", "drop": 1, "v": 1}, {"id": "p2", "drop": [1], "v": 2}))
    result = load(server, index, "-w", "1", "-id", "id", "-p", name, path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {"p1": {"id": "p1", "tag": "piped", "v": 1}, "p2": {"id": "p2", "tag": "piped", "v": 2}})


def test_pipeline_failures(server, new_index, root, pipeline):
    name = pipeline([{"remove": {"field": "drop"}}])
    lines = ndjson({"id": "q1", "v": 1}, {"id": "q2", "drop": 0, "v": 2})
    path = write_lines(root, "in.ndjson", lines)
    partial = new_index()
    result = load(server, partial, "-w", "1", "-id", "id", "-p", name, path=path)
    assert result.returncode == 1
    logged = messages(result.stderr)
    assert logged[-1] == worker_errors(BULK_FAILED)
    assert set(lines) <= set(logged)
    assert_documents(server, partial, {"q2": {"id": "q2", "v": 2}})

    missing = new_index()
    result = load(server, missing, "-w", "1", "-p", "no-such-pipeline", path=path)
    assert result.returncode == 1
    assert final(result.stderr) == worker_errors(BULK_FAILED)
    assert documents(server, missing) == {}
    assert http(server, "GET", "/_ingest/pipeline/no-such-pipeline")[0] == 404
