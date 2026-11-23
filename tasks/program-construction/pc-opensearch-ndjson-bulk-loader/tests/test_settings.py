"""Index settings the loader changes for bulk loading and sets again when it finishes."""

import pytest

from support import documents, expected_sources, index_settings, load, ok, sources, write_lines


def existing(server, index, **settings):
    ok(server, "PUT", f"/{index}", {"settings": {"index": settings}})


def test_new_index_settings_after_run(server, index):
    result = load(server, index, "-w", "1", stdin='{"v":1}\n')
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["refresh_interval"], settings["number_of_replicas"], settings["number_of_shards"]) == ("1s", "1", "1")


@pytest.mark.parametrize(("flags", "refresh"), [([], "1s"), (["-r", "7s"], "7s"), (["-r", "250ms"], "250ms"), (["-r", "-1"], "-1")])
def test_refresh_interval_set_from_flag_not_previous_value(server, index, flags, refresh):
    existing(server, index, refresh_interval="30s", number_of_replicas=2)
    result = load(server, index, "-w", "1", *flags, stdin='{"v":1}\n')
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["refresh_interval"], settings["number_of_replicas"]) == (refresh, "2")
    assert sources(server, index) == expected_sources({"v": 1})


@pytest.mark.parametrize("replicas", [0, 1, 2])
def test_zero_replicas_flag_restores_recorded_count(server, index, replicas):
    existing(server, index, number_of_replicas=replicas)
    result = load(server, index, "-w", "1", "-0", stdin='{"v":1}\n{"v":2}\n')
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["refresh_interval"], settings["number_of_replicas"]) == ("1s", str(replicas))
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 2})


def test_zero_replicas_on_new_index(server, index):
    result = load(server, index, "-w", "1", "-0", stdin='{"v":1}\n')
    assert result.returncode == 0, result.stderr
    assert index_settings(server, index)["number_of_replicas"] == "1"


@pytest.mark.parametrize(("flags", "replicas"), [(["-0"], "0"), ([], "2")])
def test_unusable_refresh_interval_stops_the_final_settings(server, index, flags, replicas):
    existing(server, index, refresh_interval="30s", number_of_replicas=2)
    result = load(server, index, "-w", "1", "-r", "bogus", *flags, stdin='{"v":1}\n')
    assert result.returncode == 0, result.stderr
    settings = index_settings(server, index)
    assert (settings["refresh_interval"], settings["number_of_replicas"]) == ("-1", replicas)
    ok(server, "POST", f"/{index}/_refresh")
    assert sources(server, index) == expected_sources({"v": 1})


def test_settings_set_after_failed_batches(server, index, root):
    existing(server, index, refresh_interval="30s", number_of_replicas=2)
    path = write_lines(root, "in.ndjson", ['{"id":"a"}', '{"id":"a"}', '{"id":"b"}'])
    result = load(server, index, "-w", "1", "-size", "1", "-id", "id", "-optype", "create", "-0", "-r", "2s", path=path)
    assert result.returncode == 1
    settings = index_settings(server, index)
    assert (settings["refresh_interval"], settings["number_of_replicas"]) == ("2s", "2")
    assert sorted(documents(server, index)) == ["a", "b"]


def test_settings_set_after_id_failures(server, index, root):
    path = write_lines(root, "in.ndjson", ['{"x":1}'])
    result = load(server, index, "-w", "1", "-id", "id", "-r", "6s", path=path)
    assert result.returncode == 1
    assert index_settings(server, index)["refresh_interval"] == "6s"
    assert documents(server, index) == {}
