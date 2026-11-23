"""Invocation, argument handling and output serialization of get/list/export."""

import json

import yaml
from harness import write_fixture
from conftest import content, failed, load, make_value, ok, run, stored, write_doc


def test_get_prints_plaintext_exactly_without_newline(ws, keys):
    # Adapted from upstream tests/002_full_key_arn.sh.
    key = keys()
    ok(run(["put", "-f", "s.yml", "password", "god", "--key-id", key["Arn"]], ws))
    result = ok(run(["get", "-f", "s.yml", "password"], ws))
    assert result.stdout == "god"


def test_flags_after_positionals_and_double_dash_secret(ws, keys):
    key = keys()
    ok(run(["put", "dash", "-f", "s.yml", "-k", key["Arn"], "--", "-x --y"], ws))
    assert stored(load(ws / "s.yml"), "dash") == [(key["Arn"], "secretbox", b"-x --y")]
    assert ok(run(["get", "dash", "-f", "s.yml"], ws)).stdout == "-x --y"


def test_environment_defaults_for_filename_and_algorithm(ws, keys):
    key = keys()
    env = {"BISCUIT_FILENAME": "env.yml", "BISCUIT_ALGORITHM": "aesgcm256"}
    ok(run(["put", "k", "v", "-k", key["Arn"]], ws, env=env))
    doc = load(ws / "env.yml")
    assert sorted(doc) == ["_keys", "k"]
    assert stored(doc, "k") == [(key["Arn"], "aesgcm256", b"v")]
    assert ok(run(["get", "k"], ws, env={"BISCUIT_FILENAME": "env.yml"})).stdout == "v"
    # An explicit flag wins over the environment default.
    ok(run(["put", "k2", "v2", "-k", key["Arn"], "-a", "secretbox"], ws, env=env))
    assert stored(load(ws / "env.yml"), "k2") == [(key["Arn"], "secretbox", b"v2")]


def test_usage_errors_leave_no_file(ws, keys):
    key = keys()
    write_fixture(ws / "in.dat", "from file")
    cases = [
        ["list"],  # --filename is required
        ["put", "-f", "s.yml", "-k", key["Arn"]],  # name is required
        ["put", "-f", "s.yml", "x", "y", "-k", key["Arn"], "-a", "rot13"],
        ["put", "-f", "s.yml", "x", "y", "-k", key["Arn"], "-p", "vault"],
        ["get", "-f", "s.yml"],
        ["get", "-f", "s.yml", "x", "extra"],
        ["bogus-command"],
    ]
    for args in cases:
        failed(run(args, ws))
        assert sorted(p.name for p in ws.iterdir()) == ["in.dat"], args


def test_secret_and_from_file_conflict(ws, keys):
    key = keys()
    ok(run(["put", "-f", "s.yml", "keep", "kept", "-k", key["Arn"]], ws))
    before = content(ws / "s.yml")
    write_fixture(ws / "in.dat", "from file")
    failed(run(["put", "-f", "s.yml", "both", "positional", "-i", "in.dat"], ws))
    failed(run(["put", "-f", "s.yml", "both", "positional", "-i", "in.dat", "-k", key["Arn"]], ws))
    assert content(ws / "s.yml") == before


def test_list_prints_every_name_but_template(ws, keys):
    key = keys()
    for name in ("alpha", "beta", "with space", "x:y"):
        ok(run(["put", "-f", "s.yml", name, "v-" + name, "-k", key["Arn"]], ws))
    lines = ok(run(["list", "-f", "s.yml"], ws)).stdout
    assert lines.endswith("\n")
    assert sorted(lines.splitlines()) == ["alpha", "beta", "with space", "x:y"]


def test_list_and_export_of_empty_file(ws):
    write_fixture(ws / "empty.yml", "")
    assert ok(run(["list", "-f", "empty.yml"], ws)).stdout == ""
    output = ok(run(["export", "-f", "empty.yml"], ws)).stdout
    assert (yaml.safe_load(output) if output.strip() else {}) == {}
    failed(run(["get", "-f", "empty.yml", "anything"], ws))
    assert (ws / "empty.yml").read_text() == ""


def test_missing_and_malformed_files_fail(ws, keys):
    # Adapted from upstream tests/015_read_nonexistent_file.sh and 016_read_empty_file.sh.
    key = keys()
    for args in (["get", "-f", "404.yml", "key"], ["list", "-f", "404.yml"], ["export", "-f", "404.yml"]):
        result = failed(run(args, ws))
        assert result.stdout == ""
    write_fixture(ws / "bad.yml", "not: [valid")
    for args in (["get", "-f", "bad.yml", "x"], ["list", "-f", "bad.yml"], ["put", "-f", "bad.yml", "x", "y", "-k", key["Arn"]]):
        failed(run(args, ws))
    assert (ws / "bad.yml").read_text() == "not: [valid"
    assert sorted(p.name for p in ws.iterdir()) == ["bad.yml"]


def test_get_unknown_name_fails_without_output_file(ws, keys):
    key = keys()
    ok(run(["put", "-f", "s.yml", "present", "here", "-k", key["Arn"]], ws))
    result = failed(run(["get", "-f", "s.yml", "absent", "-o", "out.dat"], ws))
    assert result.stdout == ""
    assert not (ws / "out.dat").exists()


def test_get_output_file_replaces_existing_content(ws, keys):
    key = keys()
    ok(run(["put", "-f", "s.yml", "short", "abc", "-k", key["Arn"]], ws))
    out = ws / "out.dat"
    write_fixture(out, "previous much longer content\n")
    result = ok(run(["get", "-f", "s.yml", "short", "-o", "out.dat"], ws))
    assert result.stdout == ""
    assert out.read_bytes() == b"abc"
    ok(run(["get", "-f", "s.yml", "short", "--output", "new.dat"], ws))
    assert (ws / "new.dat").read_bytes() == b"abc"


def test_export_serializes_plaintexts_as_yaml_strings(ws, keys):
    key = keys()
    secrets = {
        "plain": "god",
        "number": "123",
        "boolean": "true",
        "null-word": "null",
        "empty": "",
        "colon": "a: b # c",
        "padded": " spaced ",
        "multiline": "line1\nline2\n",
        "quote": "it's \"quoted\"",
        "unicode": "héllo ☃",
    }
    doc = {"_keys": [{"key_id": key["Arn"], "key_manager": "kms", "algorithm": "secretbox"}]}
    for i, (name, plaintext) in enumerate(secrets.items()):
        doc[name] = [make_value(key["Arn"], name, ("secretbox", "aesgcm256")[i % 2], plaintext.encode())]
    write_doc(ws / "s.yml", doc)
    out = ok(run(["export", "-f", "s.yml"], ws)).stdout
    exported = yaml.safe_load(out)
    assert set(exported) == set(secrets)
    for name, plaintext in secrets.items():
        assert type(exported[name]) is str and exported[name] == plaintext, name


def test_export_after_puts_with_mixed_algorithms(ws, keys):
    # Adapted from upstream tests/020_export.sh (the plaintext algorithm is out of scope).
    first, second = keys(), keys()
    ok(run(["put", "-f", "s.yml", "password", "god", "-k", first["Arn"]], ws))
    ok(run(["put", "-f", "s.yml", "username", "oreilly", "--key-id", f"{first['Arn']},{second['Arn']}", "-a", "aesgcm256"], ws))
    ok(run(["put", "-f", "s.yml", "spice", "scary", "--key-id", second["Arn"]], ws))
    out = ok(run(["export", "-f", "s.yml"], ws)).stdout
    assert yaml.safe_load(out) == {"password": "god", "username": "oreilly", "spice": "scary"}


def test_json_secrets_file_is_accepted(ws, keys):
    # Upstream tests/018_json.sh reads a JSON secrets file; YAML readers accept JSON.
    key = keys()
    doc = {"name1": [make_value(key["Arn"], "name1", "aesgcm256", b"bar")]}
    write_fixture(ws / "single.json", json.dumps(doc, indent=2))
    assert ok(run(["list", "-f", "single.json"], ws)).stdout == "name1\n"
    assert ok(run(["get", "-f", "single.json", "name1"], ws)).stdout == "bar"
    assert yaml.safe_load(ok(run(["export", "-f", "single.json"], ws)).stdout) == {"name1": "bar"}
