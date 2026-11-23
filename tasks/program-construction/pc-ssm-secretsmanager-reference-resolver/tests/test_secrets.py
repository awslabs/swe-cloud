"""Secrets Manager references: strings, fragments, version selection, ARNs, binary and regions."""

import json

import pytest

from checks import fails, ok, same


@pytest.fixture
def versions(aws):
    """Three versions: v1 (AWSPREVIOUS after v2), v2 (AWSCURRENT), v3 labelled BLUE only (YAML)."""
    full, created = aws.secret("app", json.dumps({"user": "u1", "pw": "p1", "db": {"host": "h1", "port": 1}}))
    aws.put_secret(full, json.dumps({"user": "u2", "pw": "p2", "db": {"host": "h2", "port": 2}}))
    blue = aws.put_secret(full, "user: u3\npw: p3\ndb: {host: h3, port: 3}\n", stages=["BLUE"])
    return {"name": full, "arn": created["ARN"], "v1": created["VersionId"], "v3": blue["VersionId"]}


def test_secret_string_and_fragments(versions, cli):
    name = versions["name"]
    text = f"""\
whole: ref+awssecrets://{name}
user: ref+awssecrets://{name}#/user
host: ref+awssecrets://{name}#/db/host
port: ref+awssecrets://{name}#/db/port
db: ref+awssecrets://{name}#/db
url: "https://ref+awssecrets://{name}#/db/host+:ref+awssecrets://{name}#/db/port+/"
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "whole": json.dumps({"user": "u2", "pw": "p2", "db": {"host": "h2", "port": 2}}),
        "user": "u2", "host": "h2", "port": 2, "db": {"host": "h2", "port": 2},
        "url": "https://h2:2/",
    }])
    assert ok(cli.get(f"ref+awssecrets://{name}#/db")).stdout == "map[host:h2 port:2]"


def test_version_selection(versions, cli):
    name = versions["name"]
    text = f"""\
current: ref+awssecrets://{name}?version_stage=AWSCURRENT#/pw
previous: ref+awssecrets://{name}?version_stage=AWSPREVIOUS#/pw
blue: ref+awssecrets://{name}?version_stage=BLUE#/db
blue_raw: ref+awssecrets://{name}?version_stage=BLUE
by_id: ref+awssecrets://{name}?version_id={versions['v1']}#/user
by_id_blue: ref+awssecrets://{name}?version_id={versions['v3']}#/db/port
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "current": "p2", "previous": "p1", "blue": {"host": "h3", "port": 3},
        "blue_raw": "user: u3\npw: p3\ndb: {host: h3, port: 3}\n", "by_id": "u1", "by_id_blue": 3,
    }])
    fails(cli.get(f"ref+awssecrets://{name}?version_stage=NOPE"))
    fails(cli.get(f"ref+awssecrets://{name}?version_stage=AWSCURRENT&version_id={versions['v1']}#/user"))


def test_secret_by_arn(versions, cli):
    arn = versions["arn"]
    text = f"""\
user: ref+awssecrets://{arn}#/user
host: ref+awssecrets://{arn}?version_stage=AWSPREVIOUS#/db/host
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{"user": "u2", "host": "h1"}])
    assert ok(cli.get(f"ref+awssecrets://{arn}")).stdout == json.dumps({"user": "u2", "pw": "p2", "db": {"host": "h2", "port": 2}})


def test_plain_binary_and_slash_named_secrets(aws, cli):
    plain, _ = aws.secret("plain", "plain-text-value")
    binary, _ = aws.secret("bin", binary=b"binary-bytes")
    slashed, _ = aws.secret(f"/{aws.prefix}/slashed", "slashed-value")
    proc, docs = cli.eval(f"p: ref+awssecrets://{plain}\nb: ref+awssecrets://{binary}\ns: ref+awssecrets:///{slashed[1:]}\n")
    ok(proc)
    same(docs, [{"p": "plain-text-value", "b": "binary-bytes", "s": "slashed-value"}])
    # Unlike SSM, no leading slash is implied.
    fails(cli.get(f"ref+awssecrets://{slashed[1:]}"))
    fails(cli.get(f"ref+awssecrets://{plain}#/x"))


def test_missing_secret_or_key(aws, versions, cli):
    name = versions["name"]
    fails(cli.get(f"ref+awssecrets://{aws.prefix}/absent"))
    fails(cli.get(f"ref+awssecrets://{aws.prefix}/absent#/x"))
    for fragment in ("nope", "db/nope"):
        proc, _ = cli.eval(f"v: ref+awssecrets://{name}#/{fragment}\n")
        fails(proc)
        assert ok(cli.get(f"ref+awssecrets://{name}#/{fragment}")).stdout == "<nil>"
    fails(cli.get(f"ref+awssecrets://{name}#/user/x"))


def test_null_value_is_not_a_missing_key(aws, cli):
    name, _ = aws.secret("nulls", '{"empty": null, "zero": 0, "no": false, "blank": ""}')
    r = f"ref+awssecrets://{name}#"
    proc, docs = cli.eval(f"e: {r}/empty\nz: {r}/zero\nn: {r}/no\nb: {r}/blank\n")
    ok(proc)
    same(docs, [{"e": None, "z": 0, "n": False, "b": ""}])


def test_region_parameter(aws, cli):
    name, _ = aws.secret("eu", "EU", region="eu-west-1")
    fails(cli.get(f"ref+awssecrets://{name}"))
    assert ok(cli.get(f"ref+awssecrets://{name}?region=eu-west-1")).stdout == "EU"
