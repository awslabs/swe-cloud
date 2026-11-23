"""`put`: the stored envelope, key references, the `_keys` template and failure preservation."""

import os

from harness import write_fixture
from conftest import content, failed, load, make_value, ok, opens_under, run, stored, template, values, write_doc

BOGUS_KEY = "c06320d9-aaaa-aaaa-aaaa-08263b0789d5"


def test_put_envelope_opens_independently_for_each_algorithm(ws, keys):
    key = keys()
    ok(run(["put", "-f", "s.yml", "box", "box secret", "-k", key["Arn"], "-a", "secretbox"], ws))
    ok(run(["put", "-f", "s.yml", "gcm", "gcm secret", "-k", key["Arn"], "--algorithm", "aesgcm256"], ws))
    ok(run(["put", "-f", "s.yml", "default", "default algo", "-k", key["Arn"]], ws))
    doc = load(ws / "s.yml")
    assert sorted(doc) == ["_keys", "box", "default", "gcm"]
    assert stored(doc, "box") == [(key["Arn"], "secretbox", b"box secret")]
    assert stored(doc, "gcm") == [(key["Arn"], "aesgcm256", b"gcm secret")]
    assert stored(doc, "default") == [(key["Arn"], "secretbox", b"default algo")]


def test_fresh_nonces_and_data_keys_per_put(ws, keys):
    key = keys()
    ok(run(["put", "-f", "a.yml", "same", "identical", "-k", key["Arn"], "-a", "aesgcm256"], ws))
    ok(run(["put", "-f", "b.yml", "same", "identical", "-k", key["Arn"], "-a", "aesgcm256"], ws))
    (a,), (b,) = load(ws / "a.yml")["same"], load(ws / "b.yml")["same"]
    assert a["ciphertext"] != b["ciphertext"] and a["key_ciphertext"] != b["key_ciphertext"]


def test_value_is_bound_to_its_name(ws, keys):
    key = keys()
    ok(run(["put", "-f", "s.yml", "api_key", "k-12345", "-k", key["Arn"]], ws))
    (value,) = load(ws / "s.yml")["api_key"]
    assert opens_under("api_key", value)
    assert not opens_under("renamed", value)
    # The same value moved to another name no longer decrypts through the program.
    doc = load(ws / "s.yml")
    doc["renamed"] = doc.pop("api_key")
    write_doc(ws / "moved.yml", doc)
    result = failed(run(["get", "-f", "moved.yml", "renamed"], ws))
    assert result.stdout == ""


def test_key_reference_forms_record_the_key_arn(ws, keys):
    # Adapted from upstream tests/002_full_key_arn.sh and 003_key_id_only.sh.
    alias = "alias/rt-" + os.urandom(6).hex()
    key = keys(alias=alias)
    references = {
        "by_arn": key["Arn"],
        "by_id": key["KeyId"],
        "by_alias": alias,
        "by_alias_arn": key["Arn"].split(":key/")[0] + ":" + alias,
    }
    for name, reference in references.items():
        ok(run(["put", "-f", "s.yml", name, "v-" + name, "-k", reference], ws))
    doc = load(ws / "s.yml")
    for name in references:
        assert stored(doc, name) == [(key["Arn"], "secretbox", ("v-" + name).encode())]
        assert ok(run(["get", "-f", "s.yml", name], ws)).stdout == "v-" + name


def test_first_put_creates_template_from_references_as_given(ws, keys):
    # Adapted from upstream tests/004_first_use_establishes_template.sh.
    key = keys()
    ok(run(["put", "-f", "s.yml", "password", "god", "--key-id", key["KeyId"], "-a", "aesgcm256"], ws))
    doc = load(ws / "s.yml")
    assert template(doc) == [(key["KeyId"], "aesgcm256")]
    ok(run(["put", "-f", "s.yml", "username", "oreilly"], ws))
    doc = load(ws / "s.yml")
    assert template(doc) == [(key["KeyId"], "aesgcm256")]
    assert stored(doc, "password") == [(key["Arn"], "aesgcm256", b"god")]
    assert stored(doc, "username") == [(key["Arn"], "aesgcm256", b"oreilly")]


def test_multiple_keys_produce_one_value_each(ws, keys):
    # Adapted from upstream tests/008_multiple_keys.sh with two keys in one region.
    first, second = keys(), keys()
    ok(run(["put", "-f", "s.yml", "password", "god", "--key-id", f"{first['Arn']},{second['KeyId']}"], ws))
    doc = load(ws / "s.yml")
    assert template(doc) == sorted([(first["Arn"], "secretbox"), (second["KeyId"], "secretbox")])
    assert stored(doc, "password") == sorted([(first["Arn"], "secretbox", b"god"), (second["Arn"], "secretbox", b"god")])
    ok(run(["put", "-f", "s.yml", "username", "oreilly"], ws))
    doc = load(ws / "s.yml")
    assert stored(doc, "username") == sorted([(first["Arn"], "secretbox", b"oreilly"), (second["Arn"], "secretbox", b"oreilly")])


def test_same_key_twice_keeps_both_values(ws, keys):
    key = keys()
    ok(run(["put", "-f", "s.yml", "twice", "dup", "-k", f"{key['Arn']},{key['KeyId']}", "-a", "aesgcm256"], ws))
    doc = load(ws / "s.yml")
    assert template(doc) == sorted([(key["Arn"], "aesgcm256"), (key["KeyId"], "aesgcm256")])
    assert stored(doc, "twice") == [(key["Arn"], "aesgcm256", b"dup")] * 2


def test_template_drives_put_and_overrides_algorithm_flag(ws, keys):
    first, second = keys(), keys()
    write_doc(ws / "s.yml", {
        "_keys": [
            {"key_id": first["Arn"], "key_manager": "kms", "algorithm": "aesgcm256"},
            {"key_id": second["KeyId"], "key_manager": "kms", "algorithm": "secretbox"},
        ],
    })
    ok(run(["put", "-f", "s.yml", "fromtpl", "val", "-a", "aesgcm256"], ws))
    doc = load(ws / "s.yml")
    assert sorted(doc) == ["_keys", "fromtpl"]
    assert stored(doc, "fromtpl") == sorted([(first["Arn"], "aesgcm256", b"val"), (second["Arn"], "secretbox", b"val")])
    assert template(doc) == sorted([(first["Arn"], "aesgcm256"), (second["KeyId"], "secretbox")])


def test_explicit_keys_leave_existing_template_alone(ws, keys):
    first, second = keys(), keys()
    ok(run(["put", "-f", "s.yml", "a", "1", "-k", first["KeyId"]], ws))
    ok(run(["put", "-f", "s.yml", "b", "2", "-k", second["Arn"], "-a", "aesgcm256"], ws))
    doc = load(ws / "s.yml")
    assert template(doc) == [(first["KeyId"], "secretbox")]
    assert stored(doc, "b") == [(second["Arn"], "aesgcm256", b"2")]


def test_existing_file_without_template_gets_none(ws, keys):
    # A put with --key-id seeds `_keys` only when the file did not exist yet.
    key = keys()
    write_fixture(ws / "empty.yml", "")
    ok(run(["put", "-f", "empty.yml", "x", "y", "-k", key["Arn"]], ws))
    doc = load(ws / "empty.yml")
    assert sorted(doc) == ["x"]
    assert stored(doc, "x") == [(key["Arn"], "secretbox", b"y")]
    failed(run(["put", "-f", "empty.yml", "z", "zz"], ws))
    assert content(ws / "empty.yml") == {"x": values(doc["x"])}


def test_put_replaces_all_values_of_a_name(ws, keys):
    first, second = keys(), keys()
    ok(run(["put", "-f", "s.yml", "keep", "unchanged", "-k", first["Arn"]], ws))
    ok(run(["put", "-f", "s.yml", "multi", "old", "-k", f"{first['Arn']},{second['Arn']}", "-a", "aesgcm256"], ws))
    keep_before = values(load(ws / "s.yml")["keep"])
    ok(run(["put", "-f", "s.yml", "multi", "new", "-k", second["Arn"]], ws))
    doc = load(ws / "s.yml")
    assert sorted(doc) == ["_keys", "keep", "multi"]
    assert stored(doc, "multi") == [(second["Arn"], "secretbox", b"new")]
    assert values(doc["keep"]) == keep_before
    assert template(doc) == [(first["Arn"], "secretbox")]


def test_empty_secret_and_from_file(ws, keys):
    key = keys()
    write_fixture(ws / "in.dat", b"line one\nline two\n")
    ok(run(["put", "-f", "s.yml", "empty", "-k", key["Arn"]], ws))
    ok(run(["put", "-f", "s.yml", "fromfile", "--from-file", "in.dat"], ws))
    ok(run(["put", "-f", "s.yml", "short", "-i", "in.dat", "-a", "aesgcm256"], ws))
    doc = load(ws / "s.yml")
    assert stored(doc, "empty") == [(key["Arn"], "secretbox", b"")]
    assert stored(doc, "fromfile") == [(key["Arn"], "secretbox", b"line one\nline two\n")]
    # `-a` does not apply when the template chooses the keys.
    assert stored(doc, "short") == [(key["Arn"], "secretbox", b"line one\nline two\n")]
    assert ok(run(["get", "-f", "s.yml", "empty"], ws)).stdout == ""


def test_large_binary_secret_round_trips(ws, keys):
    # Adapted from upstream tests/017_1mb.sh (the plaintext algorithm is out of scope).
    key = keys()
    data = os.urandom(1000 * 1024)
    write_fixture(ws / "1mb.dat", data)
    ok(run(["put", "-f", "s.yml", "1mb-sb", "--from-file", "1mb.dat", "--key-id", key["Arn"]], ws, timeout=60))
    ok(run(["put", "-f", "s.yml", "1mb-aes", "--from-file", "1mb.dat", "-k", key["Arn"], "-a", "aesgcm256"], ws, timeout=60))
    doc = load(ws / "s.yml")
    assert stored(doc, "1mb-sb") == [(key["Arn"], "secretbox", data)]
    assert stored(doc, "1mb-aes") == [(key["Arn"], "aesgcm256", data)]
    for name in ("1mb-sb", "1mb-aes"):
        ok(run(["get", "-f", "s.yml", name, "-o", name + ".out"], ws, timeout=60))
        assert (ws / (name + ".out")).read_bytes() == data
        assert ok(run(["get", "-f", "s.yml", name], ws, text=False, timeout=60)).stdout == data


def test_failed_puts_preserve_the_file(ws, keys):
    # Adapted from upstream tests/007_key_id_not_found.sh.
    key = keys()
    ok(run(["put", "-f", "s.yml", "gcm", "original", "-k", key["Arn"], "-a", "aesgcm256"], ws))
    before = content(ws / "s.yml")
    failed(run(["put", "-f", "s.yml", "gcm", "replacement", "-k", BOGUS_KEY], ws))
    assert content(ws / "s.yml") == before
    failed(run(["put", "-f", "s.yml", "gcm", "replacement", "-k", f"{key['Arn']},{BOGUS_KEY}"], ws))
    assert content(ws / "s.yml") == before
    failed(run(["put", "-f", "s.yml", "other", "value", "-k", BOGUS_KEY], ws))
    assert content(ws / "s.yml") == before
    assert sorted(p.name for p in ws.iterdir()) == ["s.yml"]
    assert stored(load(ws / "s.yml"), "gcm") == [(key["Arn"], "aesgcm256", b"original")]


def test_put_needs_a_key_source(ws, keys):
    # Adapted from upstream tests/005_key_is_required.sh and 007_key_id_not_found.sh.
    failed(run(["put", "-f", "store.yml", "password", "god"], ws))
    failed(run(["put", "-f", "store.yml", "password", "god", "--key-id", BOGUS_KEY], ws))
    assert list(ws.iterdir()) == []
    key = keys()
    write_doc(ws / "notemplate.yml", {"keep": [make_value(key["Arn"], "keep", "secretbox", b"k")]})
    before = content(ws / "notemplate.yml")
    failed(run(["put", "-f", "notemplate.yml", "password", "god"], ws))
    assert content(ws / "notemplate.yml") == before
