"""`get` and `export` on files built in the documented public format, including failing values."""

import base64
import os

import yaml
from conftest import content, failed, load, make_value, ok, run, write_doc


def _template(key):
    return [{"key_id": key["Arn"], "key_manager": "kms", "algorithm": "secretbox"}]


def test_get_opens_hand_built_values_of_both_algorithms(ws, keys):
    first, second = keys(), keys()
    write_doc(ws / "s.yml", {
        "_keys": _template(first),
        "gcm": [make_value(first["Arn"], "gcm", "aesgcm256", b"gcm-plain")],
        "box": [make_value(second["Arn"], "box", "secretbox", b"box-plain")],
        "bytes": [make_value(first["Arn"], "bytes", "aesgcm256", bytes(range(256)))],
    })
    assert ok(run(["get", "-f", "s.yml", "gcm"], ws)).stdout == "gcm-plain"
    assert ok(run(["get", "-f", "s.yml", "box"], ws)).stdout == "box-plain"
    assert ok(run(["get", "-f", "s.yml", "bytes"], ws, text=False)).stdout == bytes(range(256))


def test_get_falls_back_to_another_value(ws, keys):
    # Adapted from upstream tests/009_multiple_regions_one_fails.sh with two keys in one region.
    first, second = keys(), keys()
    broken_cipher = dict(make_value(first["Arn"], "pw", "secretbox", b"x"),
                         ciphertext=base64.b64encode(os.urandom(48)).decode())
    wrong_name = make_value(first["Arn"], "pw", "aesgcm256", b"y", context="elsewhere")
    good = make_value(second["Arn"], "pw", "aesgcm256", b"second-wins")
    for order in ([broken_cipher, good], [good, broken_cipher], [wrong_name, broken_cipher, good]):
        write_doc(ws / "s.yml", {"_keys": _template(first), "pw": order})
        assert ok(run(["get", "-f", "s.yml", "pw"], ws)).stdout == "second-wins"


def test_key_id_region_routes_decryption(ws, keys):
    # A value whose key_id names an unknown region cannot be opened; another value still can.
    first, second = keys(), keys()
    bad_region = make_value(first["Arn"], "pw", "secretbox", b"god")
    bad_region["key_id"] = bad_region["key_id"].replace("us-east-1", "xxx")
    good = make_value(second["Arn"], "pw", "secretbox", b"god")
    write_doc(ws / "one.yml", {"pw": [bad_region, good]})
    for extra in ([], ["-p", "xxx"], ["--aws-region-priority", "us-east-1"]):
        assert ok(run(["get", "-f", "one.yml", "pw", *extra], ws)).stdout == "god"
    write_doc(ws / "none.yml", {"pw": [bad_region]})
    result = failed(run(["get", "-f", "none.yml", "pw"], ws))
    assert result.stdout == ""


def test_get_fails_when_no_value_opens(ws, keys):
    # Adapted from upstream tests/019_multiple_regions_both_fail.sh.
    key = keys()
    write_doc(ws / "s.yml", {
        "ctx": [make_value(key["Arn"], "pw", "secretbox", b"nope", context="other")],
        "tamper": [dict(make_value(key["Arn"], "tamper", "aesgcm256", b"nope"),
                        ciphertext=base64.b64encode(os.urandom(40)).decode())],
    })
    before = content(ws / "s.yml")
    for name in ("ctx", "tamper"):
        result = failed(run(["get", "-f", "s.yml", name, "-o", name + ".out"], ws))
        assert result.stdout == ""
        assert not (ws / (name + ".out")).exists()
    assert content(ws / "s.yml") == before


def test_secretbox_and_gcm_layouts_are_not_interchangeable(ws, keys):
    # Declaring the other algorithm for a value makes it undecryptable.
    key = keys()
    gcm = make_value(key["Arn"], "a", "aesgcm256", b"plain-a")
    box = make_value(key["Arn"], "b", "secretbox", b"plain-b")
    write_doc(ws / "s.yml", {"a": [dict(gcm, algorithm="secretbox")], "b": [dict(box, algorithm="aesgcm256")]})
    failed(run(["get", "-f", "s.yml", "a"], ws))
    failed(run(["get", "-f", "s.yml", "b"], ws))


def test_export_skips_failures_and_reports_them(ws, keys):
    first, second = keys(), keys()
    write_doc(ws / "s.yml", {
        "_keys": _template(first),
        "fine": [make_value(first["Arn"], "fine", "aesgcm256", b"ok-1")],
        "fallback": [make_value(first["Arn"], "fallback", "secretbox", b"z", context="nope"),
                     make_value(second["Arn"], "fallback", "secretbox", b"ok-2")],
        "broken": [make_value(first["Arn"], "broken", "secretbox", b"lost", context="nope")],
    })
    result = failed(run(["export", "-f", "s.yml"], ws))
    assert yaml.safe_load(result.stdout) == {"fine": "ok-1", "fallback": "ok-2"}


def test_export_succeeds_when_every_secret_opens(ws, keys):
    key = keys()
    write_doc(ws / "s.yml", {
        "_keys": _template(key),
        "one": [make_value(key["Arn"], "one", "aesgcm256", b"1st")],
        "two": [make_value(key["Arn"], "two", "secretbox", b"2nd")],
    })
    result = ok(run(["export", "-f", "s.yml", "-p", "us-east-1"], ws))
    assert yaml.safe_load(result.stdout) == {"one": "1st", "two": "2nd"}
    assert load(ws / "s.yml")["_keys"] == _template(key)


def test_export_status_counts_every_failed_attempt(ws, keys):
    # Values are tried in file order; a failed attempt makes export fail even when a later value opens.
    key = keys()
    bad = make_value(key["Arn"], "pw", "secretbox", b"x", context="elsewhere")
    good = make_value(key["Arn"], "pw", "aesgcm256", b"opened")
    write_doc(ws / "good_first.yml", {"pw": [good, bad]})
    result = ok(run(["export", "-f", "good_first.yml"], ws))
    assert yaml.safe_load(result.stdout) == {"pw": "opened"}
    write_doc(ws / "bad_first.yml", {"pw": [bad, good]})
    result = failed(run(["export", "-f", "bad_first.yml"], ws))
    assert yaml.safe_load(result.stdout) == {"pw": "opened"}
    assert ok(run(["get", "-f", "bad_first.yml", "pw"], ws)).stdout == "opened"
