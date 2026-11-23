"""Invocation, argument validation and input reading."""

import gzip
import re

import pytest

from harness import run_program, write_fixture
from support import assert_documents, documents, exists, final, index_settings, load, messages, ndjson, sources, expected_sources, write_lines


def test_version_prints_and_exits():
    result = run_program(["-v"])
    assert result.returncode == 0
    assert result.stdout == "0.7.35\n"
    assert result.stderr == ""


def test_help_lists_flags():
    result = run_program(["-h"])
    assert result.returncode == 0
    assert result.stdout == ""
    for flag in ("0", "c", "id", "index", "mapping", "optype", "p", "purge", "purge-pause", "r", "server", "size", "skipbroken", "timeout", "type", "v", "w", "z", "seed"):
        assert re.search(rf"^  -{re.escape(flag)}(\s|$)", result.stderr, re.M), flag


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["-bogus"], "flag provided but not defined: -bogus"),
        (["-index", "x", "-purge-pause", "soon"], 'invalid value "soon" for flag -purge-pause: parse error'),
        (["-index", "x", "-size", "abc"], 'invalid value "abc" for flag -size: parse error'),
    ],
)
def test_flag_parse_errors(args, message):
    result = run_program(args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines()[0] == message


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "index name required"),
        (["-index", "x", "-w", "0"], "no workers configured"),
        (["-index", "x", "-size", "0"], "cannot use zero batch size"),
        (["-w", "0", "-size", "0"], "no workers configured"),
        (["-size", "0"], "cannot use zero batch size"),
    ],
)
def test_configuration_errors(args, message):
    result = run_program(args)
    assert result.returncode == 1
    assert result.stdout == ""
    assert messages(result.stderr) == [message]
    assert re.match(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ", result.stderr)


@pytest.mark.parametrize("flags", [["-w", "0"], ["-size", "0"]])
def test_configuration_error_touches_no_index(server, index, root, flags):
    path = write_lines(root, "in.ndjson", ndjson({"v": 1}))
    result = load(server, index, *flags, path=path)
    assert result.returncode == 1
    assert not exists(server, index)


def test_missing_input_file(server, index, root):
    result = load(server, index, "missing.ndjson", cwd=root)
    assert result.returncode == 1
    assert messages(result.stderr) == ["open missing.ndjson: no such file or directory"]
    assert not exists(server, index)


def test_reads_stdin_when_no_file(server, index):
    result = load(server, index, "-id", "id", "-w", "1", stdin='{"id":"s1","n":1}\n{"id":"s2","n":2}\n')
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""
    assert_documents(server, index, {"s1": {"id": "s1", "n": 1}, "s2": {"id": "s2", "n": 2}})


def test_empty_input_creates_index(server, index):
    result = load(server, index, "-w", "1", stdin="")
    assert result.returncode == 0, result.stderr
    assert documents(server, index) == {}
    settings = index_settings(server, index)
    assert settings["refresh_interval"] == "1s"
    assert settings["number_of_replicas"] == "1"


def test_unterminated_last_line_is_ignored(server, new_index, root):
    two = new_index()
    path = write_lines(root, "two.ndjson", ['{"id":"a1"}', '{"id":"a2"}'], newline_at_end=False)
    result = load(server, two, "-id", "id", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert_documents(server, two, {"a1": {"id": "a1"}})

    one = new_index()
    path = write_lines(root, "one.ndjson", ['{"id":"only"}   '], newline_at_end=False)
    result = load(server, one, "-id", "id", "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert documents(server, one) == {}
    assert index_settings(server, one)["refresh_interval"] == "1s"


def test_blank_lines_and_surrounding_whitespace(server, index, root):
    path = root / "ws.ndjson"
    write_fixture(path, '  {"v": 1}  \r\n\t\n\n   \n{"v": 2}\r\n', mode=0o644)
    result = load(server, index, "-w", "1", path=path)
    assert result.returncode == 0, result.stderr
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 2})


def test_auto_ids_for_every_line(server, index, root):
    path = write_lines(root, "dup.ndjson", ndjson({"v": 1}, {"v": 1}, {"v": 2}))
    result = load(server, index, "-size", "1", "-w", "2", path=path)
    assert result.returncode == 0, result.stderr
    assert sources(server, index) == expected_sources({"v": 1}, {"v": 1}, {"v": 2})


def test_gzip_file(server, index, root):
    path = root / "in.ndjson.gz"
    write_fixture(path, gzip.compress(b'{"id":"z1","g":1}\n\n{"id":"z2","g":2}\n{"id":"z3","g":3}'), mode=0o644)
    result = load(server, index, "-z", "-id", "id", "-size", "1", "-w", "2", path=path)
    assert result.returncode == 0, result.stderr
    # The final line has no newline and is not loaded, as with plain input.
    assert_documents(server, index, {"z1": {"id": "z1", "g": 1}, "z2": {"id": "z2", "g": 2}})


def test_gzip_stdin(server, index):
    data = gzip.compress(b'{"g":1}\n{"g":2}\n')
    result = load(server, index, "-z", "-w", "1", stdin=data, text=False)
    assert result.returncode == 0, result.stderr
    assert sources(server, index) == expected_sources({"g": 1}, {"g": 2})


def test_gzip_flag_on_plain_input(server, index, root):
    path = write_lines(root, "plain.ndjson", ndjson(*({"v": i, "pad": "x" * 20} for i in range(3))))
    result = load(server, index, "-z", "-w", "1", "-r", "3s", path=path)
    assert result.returncode == 1
    assert final(result.stderr) == "failed to create gzip reader: gzip: invalid header"
    assert documents(server, index) == {}
    assert index_settings(server, index)["refresh_interval"] == "3s"


def test_server_without_scheme(server, index):
    hostport = server.removeprefix("http://")
    result = run_program(["-server", hostport, "-index", index, "-w", "1"], stdin='{"v":1}\n')
    assert result.returncode == 0, result.stderr
    assert sources(server, index) == expected_sources({"v": 1})


def test_repeated_server_flag(server, index, root):
    hostport = server.removeprefix("http://")
    path = write_lines(root, "in.ndjson", ndjson(*({"id": f"r{i}", "i": i} for i in range(12))))
    result = run_program(["-server", hostport, "-server", server, "-index", index, "-id", "id", "-0", "-size", "1", "-w", "3", path.name], cwd=str(root))
    assert result.returncode == 0, result.stderr
    assert_documents(server, index, {f"r{i}": {"id": f"r{i}", "i": i} for i in range(12)})
    settings = index_settings(server, index)
    assert settings["refresh_interval"] == "1s"
    assert settings["number_of_replicas"] == "1"


def test_unreachable_server(index):
    result = run_program(["-server", "http://127.0.0.1:1", "-index", index, "-w", "1", "-timeout", "2s"], stdin='{"v":1}\n', timeout=50)
    assert result.returncode == 1
    message = final(result.stderr)
    assert message.startswith(f'Get "http://127.0.0.1:1/{index}": '), message
    assert "connection refused" in message
