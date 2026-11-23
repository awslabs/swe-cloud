"""Multi-step sequences across put, get, list, export, kms init and kms deprovision."""

import base64

import pytest
import yaml
from harness import write_fixture
from conftest import alias_entry, content, failed, load, ok, run, stack, stored, template, values, write_doc

R = ["-r", "us-east-1"]


def test_template_can_be_removed(ws, keys):
    # Adapted from upstream tests/006_template_can_be_removed.sh.
    key = keys()
    ok(run(["put", "-f", "store.yml", "password", "god", "--key-id", key["KeyId"]], ws))
    ok(run(["put", "-f", "store.yml", "username", "oreilly"], ws))
    text = (ws / "store.yml").read_text()
    write_fixture(ws / "store.yml", text.replace("_keys", "_removed"))
    assert ok(run(["get", "-f", "store.yml", "password"], ws)).stdout == "god"
    assert ok(run(["get", "-f", "store.yml", "username"], ws)).stdout == "oreilly"
    before = content(ws / "store.yml")
    failed(run(["put", "-f", "store.yml", "spice", "scary"], ws))
    assert content(ws / "store.yml") == before
    # Only the reserved `_keys` name is hidden from list.
    assert sorted(ok(run(["list", "-f", "store.yml"], ws)).stdout.splitlines()) == ["_removed", "password", "username"]
    # An explicit key still works, and no template is recreated in an existing file.
    ok(run(["put", "-f", "store.yml", "spice", "scary", "-k", key["Arn"]], ws))
    doc = load(ws / "store.yml")
    assert sorted(doc) == ["_removed", "password", "spice", "username"]
    assert stored(doc, "spice") == [(key["Arn"], "secretbox", b"scary")]


def test_corrupted_value_is_repaired_by_a_new_put(ws, keys):
    # Adapted from upstream tests/009_multiple_regions_one_fails.sh with two keys in one region.
    first, second = keys(), keys()
    ok(run(["put", "-f", "store.yml", "password", "god", "--key-id", f"{first['Arn']},{second['Arn']}", "-a", "aesgcm256"], ws))
    ok(run(["put", "-f", "store.yml", "username", "oreilly"], ws))
    doc = load(ws / "store.yml")
    for name in ("password", "username"):
        for value in doc[name]:
            if value["key_id"] == first["Arn"]:
                blob = bytearray(base64.b64decode(value["ciphertext"]))
                blob[0] ^= 0xFF
                value["ciphertext"] = base64.b64encode(bytes(blob)).decode()
    write_doc(ws / "corrupt.yml", doc)
    assert ok(run(["get", "-f", "corrupt.yml", "password"], ws)).stdout == "god"
    assert ok(run(["get", "-f", "corrupt.yml", "username"], ws)).stdout == "oreilly"
    # Export's status depends on whether a corrupted value is tried first (value order follows put
    # completion), so only its output is compared here; test_decryption.py pins the ordered cases.
    assert yaml.safe_load(run(["export", "-f", "corrupt.yml"], ws).stdout) == {"password": "god", "username": "oreilly"}
    ok(run(["put", "-f", "corrupt.yml", "password", "fresh"], ws))
    repaired = load(ws / "corrupt.yml")
    assert stored(repaired, "password") == sorted([(first["Arn"], "aesgcm256", b"fresh"), (second["Arn"], "aesgcm256", b"fresh")])
    assert values(repaired["username"]) == values(doc["username"])


@pytest.mark.timeout(240)
def test_two_labels_share_one_file(ws, labels):
    blue, green = labels(), labels()
    ok(run(["kms", "init", *R, "-l", blue, "-f", "s.yml"], ws, timeout=150))
    ok(run(["put", "-f", "s.yml", "only_blue", "b1"], ws))
    ok(run(["kms", "init", *R, "-l", green, "-f", "s.yml", "-a", "aesgcm256"], ws, timeout=150))
    blue_alias, green_alias = alias_entry(blue), alias_entry(green)
    assert blue_alias["TargetKeyId"] != green_alias["TargetKeyId"]
    doc = load(ws / "s.yml")
    assert template(doc) == sorted([(blue_alias["AliasArn"], "secretbox"), (green_alias["AliasArn"], "aesgcm256")])
    ok(run(["put", "-f", "s.yml", "both", "b2"], ws))
    doc = load(ws / "s.yml")
    blue_arn = doc["only_blue"][0]["key_id"]
    assert blue_arn.endswith(":key/" + blue_alias["TargetKeyId"])
    green_arn = blue_arn.rsplit("/", 1)[0] + "/" + green_alias["TargetKeyId"]
    assert stored(doc, "only_blue") == [(blue_arn, "secretbox", b"b1")]
    assert stored(doc, "both") == sorted([(blue_arn, "secretbox", b"b2"), (green_arn, "aesgcm256", b"b2")])
    assert sorted(ok(run(["list", "-f", "s.yml"], ws)).stdout.splitlines()) == ["both", "only_blue"]
    assert yaml.safe_load(ok(run(["export", "-f", "s.yml"], ws)).stdout) == {"only_blue": "b1", "both": "b2"}
    # Removing one label's resources leaves the other label and the file alone.
    before = content(ws / "s.yml")
    ok(run(["kms", "deprovision", *R, "-l", blue, "--destructive"], ws, timeout=150))
    assert alias_entry(blue) is None and stack(blue) is None
    assert alias_entry(green) == green_alias and stack(green)["StackStatus"] == "CREATE_COMPLETE"
    assert content(ws / "s.yml") == before
    assert ok(run(["get", "-f", "s.yml", "both"], ws)).stdout == "b2"
    ok(run(["kms", "deprovision", *R, "-l", green, "--destructive"], ws, timeout=150))
    assert alias_entry(green) is None and stack(green) is None


@pytest.mark.timeout(180)
def test_init_then_explicit_key_put_keeps_template(ws, keys, labels):
    label = labels()
    extra = keys()
    ok(run(["kms", "init", *R, "-l", label, "-f", "s.yml"], ws, timeout=150))
    alias_arn = alias_entry(label)["AliasArn"]
    ok(run(["put", "-f", "s.yml", "explicit", "e", "-k", extra["KeyId"], "-a", "aesgcm256"], ws))
    ok(run(["put", "-f", "s.yml", "implicit", "i", "-a", "aesgcm256"], ws))
    doc = load(ws / "s.yml")
    assert template(doc) == [(alias_arn, "secretbox")]
    assert stored(doc, "explicit") == [(extra["Arn"], "aesgcm256", b"e")]
    (implicit,) = doc["implicit"]
    assert implicit["key_id"].endswith(":key/" + alias_entry(label)["TargetKeyId"])
    assert stored(doc, "implicit") == [(implicit["key_id"], "secretbox", b"i")]
