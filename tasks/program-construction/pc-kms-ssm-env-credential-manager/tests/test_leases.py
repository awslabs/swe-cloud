"""aws-sts lease backend: minting credentials, the ledger seen through `lease list`, reuse by exec, revocation."""

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone

from pathlib import Path

import boto3
import pytest

import fixture_io
from support import child_env, failed, ok

CREDS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@pytest.fixture
def role(iam_roles):
    return iam_roles()


def lease_config(arn, extra="", backend="sts"):
    return f"""\
root = true
{extra}
[leases.{backend}]
type = "aws-sts"
region = "us-east-1"
role_arn = "{arn}"

[secrets]
APP = {{ default = "app-value" }}
"""


def identity(creds):
    return boto3.client("sts", aws_access_key_id=creds["AWS_ACCESS_KEY_ID"], aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"],
                        aws_session_token=creds["AWS_SESSION_TOKEN"]).get_caller_identity()


def parse_time(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert value.tzinfo is not None
    return value


def listing(project, *flags):
    rows = json.loads(ok(project.run("lease", "list", "--json", *flags)).stdout)
    assert isinstance(rows, list)
    return rows


def test_lease_create_json(project, role):
    name, arn = role
    project.write("fnox.toml", lease_config(arn))
    started = datetime.now(timezone.utc)
    doc = json.loads(ok(project.run("lease", "create", "sts", "--duration", "15m", "--format", "json")).stdout)
    assert set(doc) == {"backend", "lease_id", "expires_at", *CREDS}
    assert doc["backend"] == "sts"
    assert all(isinstance(doc[k], str) and doc[k] for k in CREDS)
    expires = parse_time(doc["expires_at"])
    assert timedelta(minutes=14) <= expires - started <= timedelta(minutes=16)
    assert re.fullmatch(rf"arn:aws:sts::000000000000:assumed-role/{re.escape(name)}/[^/]+", identity(doc)["Arn"])


def test_lease_create_env_and_shell_formats_and_label(project, role):
    name, arn = role
    project.write("fnox.toml", lease_config(arn))
    out = ok(project.run("lease", "create", "sts", "-d", "1h", "-f", "env", "-l", "ci-job")).stdout
    exported = dict(re.fullmatch(r"export (\w+)=(.*)", line).groups() for line in out.splitlines() if line.strip())
    assert set(exported) == set(CREDS)
    creds = {k: v.strip("'\"") for k, v in exported.items()}
    assert re.fullmatch(rf"arn:aws:sts::000000000000:assumed-role/{re.escape(name)}/[^/]+", identity(creds)["Arn"])
    shell = ok(project.run("lease", "create", "sts", "--format", "shell")).stdout
    assert all(k in shell for k in CREDS)
    rows = listing(project)
    assert sorted(r["label"] for r in rows) == ["ci-job", "fnox-lease"]
    labelled = next(r for r in rows if r["label"] == "ci-job")
    assert timedelta(minutes=59) <= parse_time(labelled["expires_at"]) - parse_time(labelled["created_at"]) <= timedelta(minutes=61)


def test_lease_list_records(project, role):
    _, arn = role
    project.write("fnox.toml", lease_config(arn))
    assert listing(project) == []
    created = []
    for duration in ("15m", "2h30m"):
        created.append(json.loads(ok(project.run("lease", "create", "sts", "-d", duration, "-f", "json")).stdout))
    rows = listing(project)
    assert [r["lease_id"] for r in rows] == [c["lease_id"] for c in created]
    for row, doc in zip(rows, created, strict=True):
        assert set(row) == {"lease_id", "backend", "label", "status", "created_at", "expires_at"}
        assert (row["backend"], row["label"], row["status"]) == ("sts", "fnox-lease", "active")
        assert parse_time(row["expires_at"]) == parse_time(doc["expires_at"])
    assert timedelta(hours=2, minutes=29) <= parse_time(rows[1]["expires_at"]) - parse_time(rows[1]["created_at"]) <= timedelta(hours=2, minutes=31)
    assert listing(project, "--expired") == []
    assert [r["lease_id"] for r in listing(project, "--active")] == [c["lease_id"] for c in created]
    assert "sts" in ok(project.run("lease", "list")).stdout


def test_lease_revoke_by_id_backend_and_all(project, role):
    _, arn = role
    project.write("fnox.toml", lease_config(arn))
    ids = [json.loads(ok(project.run("lease", "create", "sts", "-f", "json")).stdout)["lease_id"] for _ in range(3)]
    ok(project.run("lease", "revoke", ids[0]))
    status = {r["lease_id"]: r["status"] for r in listing(project)}
    assert status == {ids[0]: "revoked", ids[1]: "active", ids[2]: "active"}
    ok(project.run("lease", "revoke", ids[0]))  # already revoked: still succeeds
    assert [r["lease_id"] for r in listing(project, "--active")] == ids[1:]
    failed(project.run("lease", "revoke", "no-such-lease"))
    common = ids[1]
    while not all(i.startswith(common) for i in ids):
        common = common[:-1]
    if common:  # an ambiguous prefix revokes nothing
        failed(project.run("lease", "revoke", common))
        assert [r["lease_id"] for r in listing(project, "--active")] == ids[1:]
    # Shortest unique proper prefix when the ID shape has one, otherwise the full ID.
    unique = next((ids[1][:n] for n in range(len(common) + 1, len(ids[1]))
                   if sum(i.startswith(ids[1][:n]) for i in ids) == 1), ids[1])
    ok(project.run("lease", "revoke", unique))
    assert [r["lease_id"] for r in listing(project, "--active")] == ids[2:]
    failed(project.run("lease", "revoke"))
    ok(project.run("lease", "revoke", "sts"))
    assert {r["status"] for r in listing(project)} == {"revoked"}
    assert listing(project, "--active") == []
    ok(project.run("lease", "revoke", "--all"))
    assert len(listing(project)) == 3


def test_lease_create_rejections(project, role):
    _, arn = role
    project.write("fnox.toml", lease_config(arn))
    failed(project.run("lease", "create", "nosuch"))
    failed(project.run("lease", "create", "sts", "-d", "13h"))
    failed(project.run("lease", "create", "sts", "-d", "bogus"))
    failed(project.run("lease", "create", "sts", "-d", "10s"))  # below the STS minimum
    assert listing(project) == []
    project.write("fnox.toml", lease_config("arn:aws:iam::000000000000:role/sem-never-created-role"))
    failed(project.run("lease", "create", "sts"))
    assert listing(project) == []


def test_exec_injects_and_reuses_cached_lease(project, role):
    name, arn = role
    project.write("fnox.toml", lease_config(arn))
    # The longest-lived lease is created first, so "newest" and "latest expiry" differ.
    long = json.loads(ok(project.run("lease", "create", "sts", "-d", "1h", "-f", "json", "-l", "longer")).stdout)
    short = json.loads(ok(project.run("lease", "create", "sts", "-d", "15m", "-f", "json")).stdout)
    middle = json.loads(ok(project.run("lease", "create", "sts", "-d", "30m", "-f", "json")).stdout)
    values, _ = child_env(project, names=[*CREDS, "APP"])
    assert values == {**{k: long[k] for k in CREDS}, "APP": "app-value"}
    assert len({short["AWS_ACCESS_KEY_ID"], middle["AWS_ACCESS_KEY_ID"], long["AWS_ACCESS_KEY_ID"]}) == 3
    env = json.loads(ok(project.run("env", "--json")).stdout)
    assert {k: env["set"][k] for k in CREDS} == {k: long[k] for k in CREDS} and env["set"]["APP"] == "app-value"
    assert env["leases"] == ["sts"]
    assert len(listing(project)) == 3
    # Once the longest lease is revoked, the next-latest expiry (30m) is reused, still without minting.
    ok(project.run("lease", "revoke", long["lease_id"]))
    values, _ = child_env(project, names=list(CREDS))
    assert values == {k: middle[k] for k in CREDS}
    assert len(listing(project)) == 3


def test_exec_mints_new_lease_after_revocation(project, role):
    name, arn = role
    project.write("fnox.toml", lease_config(arn))
    first = json.loads(ok(project.run("lease", "create", "sts", "-f", "json")).stdout)
    ok(project.run("lease", "revoke", "--all"))
    values, _ = child_env(project, names=list(CREDS))
    assert values["AWS_ACCESS_KEY_ID"] != first["AWS_ACCESS_KEY_ID"]
    assert identity(values)["Arn"].startswith(f"arn:aws:sts::000000000000:assumed-role/{name}/")
    rows = listing(project)
    assert len(rows) == 2 and rows[0]["status"] == "revoked" and rows[1]["status"] == "active"
    # The minted lease is cached: a second exec reuses it.
    again, _ = child_env(project, names=list(CREDS))
    assert again == values
    assert len(listing(project)) == 2


def test_backend_change_invalidates_cached_lease(project, iam_roles):
    _, arn1 = iam_roles("one")
    name2, arn2 = iam_roles("two")
    project.write("fnox.toml", lease_config(arn1))
    first = json.loads(ok(project.run("lease", "create", "sts", "-f", "json")).stdout)
    project.write("fnox.toml", lease_config(arn2))
    values, _ = child_env(project, names=list(CREDS))
    assert values["AWS_ACCESS_KEY_ID"] != first["AWS_ACCESS_KEY_ID"]
    assert identity(values)["Arn"].startswith(f"arn:aws:sts::000000000000:assumed-role/{name2}/")


def test_exec_fails_when_lease_cannot_be_minted(project):
    project.write("fnox.toml", lease_config("arn:aws:iam::000000000000:role/sem-never-created-role"))
    marker = project.path("ran")
    failed(project.run("exec", "--", "touch", str(marker)))
    assert not marker.exists()


def state_files(root):
    """Contents of regular files below a candidate-writable directory, never following links."""
    found = {}
    for directory, _, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if stat.S_ISREG(os.lstat(path).st_mode):
                with fixture_io.open_file(path, "rb") as stream:
                    found[path] = stream.read()
    return found


def test_ledger_lives_in_state_dir_and_is_encrypted_with_default_provider(project, role, kms_key):
    _, arn = role
    state = project.mkdir("state", base=project.root)
    env = {"FNOX_STATE_DIR": str(state)}
    plain_home = project.root / "home"
    project.write("fnox.toml", lease_config(arn))
    plain = json.loads(ok(project.run("lease", "create", "sts", "-f", "json", env=env)).stdout)
    files = state_files(state)
    assert any(plain["lease_id"].encode() in data for data in files.values())
    assert not any(plain["lease_id"].encode() in data for data in state_files(plain_home).values())
    # With an encryption default_provider, cached credentials are not stored in the clear.
    project.write("fnox.toml", lease_config(arn, f'default_provider = "kms"\n\n[providers.kms]\ntype = "aws-kms"\n'
                                                 f'key_id = "{kms_key}"\nregion = "us-east-1"\n', backend="enc"))
    enc = json.loads(ok(project.run("lease", "create", "enc", "-f", "json", env=env)).stdout)
    files = state_files(state)
    assert any(enc["lease_id"].encode() in data for data in files.values())
    for key in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert not any(enc[key].encode() in data for data in files.values())
    values, _ = child_env(project, names=list(CREDS), env=env)
    assert values == {k: enc[k] for k in CREDS}


def json_stream(text):
    decoder, docs, index = json.JSONDecoder(), [], 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        doc, index = decoder.raw_decode(text, index)
        docs.append(doc)
    return docs


def test_lease_create_for_every_backend(project, role):
    name, arn = role
    config = lease_config(arn, backend="one") + f'\n[leases.two]\ntype = "aws-sts"\nregion = "us-east-1"\nrole_arn = "{arn}"\nduration = "30m"\n'
    project.write("fnox.toml", config)
    started = datetime.now(timezone.utc)
    for args in (["-a"], []):
        docs = json_stream(ok(project.run("lease", "create", *args, "-f", "json")).stdout)
        assert sorted(d["backend"] for d in docs) == ["one", "two"]
        two = next(d for d in docs if d["backend"] == "two")
        assert timedelta(minutes=29) <= parse_time(two["expires_at"]) - started <= timedelta(minutes=31)
        assert all(identity(d)["Arn"].startswith(f"arn:aws:sts::000000000000:assumed-role/{name}/") for d in docs)
    assert sorted(r["backend"] for r in listing(project)) == ["one", "one", "two", "two"]
