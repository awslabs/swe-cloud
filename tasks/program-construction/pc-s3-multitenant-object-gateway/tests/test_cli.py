"""Gateway startup and the admin client (account bootstrap, account store record layout)."""

import json
import socket

from conftest import backend, bucket_names, error_of, is_number, upstream_buckets


def assert_account(record, access, secret, role, ids):
    assert set(record) == {"access", "secret", "role", "userID", "groupID", "projectID"}, record
    assert record["access"] == access and type(record["access"]) is str
    assert record["secret"] == secret and type(record["secret"]) is str
    assert record["role"] == role and type(record["role"]) is str
    for field, expected in zip(("userID", "groupID", "projectID"), ids):
        assert is_number(record[field]) and record[field] == expected, (field, record[field])


def table_rows(output):
    """Whitespace-split lines of the list-users table."""
    return [line.split() for line in output.splitlines() if line.strip()]


def test_startup_fails_without_metadata_bucket(stopped_gateway):
    # The account bucket exists, the metadata bucket does not.
    backend().create_bucket(Bucket=stopped_gateway.iamb)
    proc = stopped_gateway.launch()
    try:
        proc.wait(timeout=20)
    except Exception:
        listening = True
        try:
            socket.create_connection(("127.0.0.1", stopped_gateway.port), 0.5).close()
        except OSError:
            listening = False
        raise AssertionError(f"gateway kept running without its metadata bucket (listening={listening})")
    assert proc.returncode != 0
    assert stopped_gateway.meta not in upstream_buckets()


def test_create_user_writes_account_records(gateway):
    a, b = f"acct-a-{gateway.suffix}", f"acct-b-{gateway.suffix}"
    first = gateway.admin("create-user", "--access", a, "--secret", "first-secret", "--role", "userplus")
    assert first.returncode == 0, first.stderr
    doc = gateway.users()
    assert set(doc) == {"accessAccounts"}
    assert set(doc["accessAccounts"]) == {a}
    assert_account(doc["accessAccounts"][a], a, "first-secret", "userplus", (0, 0, 0))

    second = gateway.admin(
        "create-user", "--access", b, "--secret", "second-secret", "--role", "userplus",
        "--user-id", "1001", "--group-id", "2002", "--project-id", "3",
    )
    assert second.returncode == 0, second.stderr
    doc = gateway.users()
    assert set(doc) == {"accessAccounts"}
    assert set(doc["accessAccounts"]) == {a, b}
    assert_account(doc["accessAccounts"][a], a, "first-secret", "userplus", (0, 0, 0))
    assert_account(doc["accessAccounts"][b], b, "second-secret", "userplus", (1001, 2002, 3))


def test_list_users_prints_one_row_per_account(gateway):
    empty = gateway.admin("list-users")
    assert empty.returncode == 0, empty.stderr
    assert not any(gateway.suffix in line for line in empty.stdout.splitlines())

    a, b = f"rowa-{gateway.suffix}", f"rowb-{gateway.suffix}"
    gateway.create_user(a, "hidden-secret-one")
    gateway.create_user(b, "hidden-secret-two", "--user-id", "41", "--group-id", "42", "--project-id", "43")
    listed = gateway.admin("list-users")
    assert listed.returncode == 0, listed.stderr
    rows = [row for row in table_rows(listed.stdout) if any(gateway.suffix in cell for cell in row)]
    assert sorted(rows) == sorted([[a, "userplus", "0", "0", "0"], [b, "userplus", "41", "42", "43"]])
    assert "hidden-secret" not in listed.stdout


def test_duplicate_account_is_rejected(tenants):
    a, _ = tenants
    gw = a.gateway
    before = gw.users()
    result = gw.admin("create-user", "--access", a.access, "--secret", "replacement", "--role", "userplus")
    assert result.returncode != 0
    assert gw.users() == before
    assert before["accessAccounts"][a.access]["secret"] == a.secret
    # The original secret still authenticates; the rejected one does not.
    assert bucket_names(a.s3) == []
    assert error_of(lambda: gw.client(a.access, "replacement").list_buckets()) == (403, "SignatureDoesNotMatch")


def test_invalid_role_is_rejected(tenants):
    gw = tenants[0].gateway
    before = gw.users()
    bad = f"bad-{gw.suffix}"
    result = gw.admin("create-user", "--access", bad, "--secret", "x", "--role", "superuser")
    assert result.returncode != 0
    assert gw.users() == before
    assert error_of(lambda: gw.client(bad, "x").list_buckets()) == (403, "InvalidAccessKeyId")


def test_admin_requires_root_credentials(tenants):
    a, b = tenants
    gw = a.gateway
    before = gw.users()
    intruder = f"intruder-{gw.suffix}"
    as_tenant = gw.admin("create-user", "--access", intruder, "--secret", "x", "--role", "userplus",
                         access=a.access, secret=a.secret)
    assert as_tenant.returncode != 0
    assert gw.users() == before
    listing = gw.admin("list-users", access=b.access, secret=b.secret)
    assert listing.returncode != 0
    assert a.access not in listing.stdout
    wrong_secret = gw.admin("list-users", secret="not-the-root-secret")
    assert wrong_secret.returncode != 0
    assert a.access not in wrong_secret.stdout
    forged = gw.admin("create-user", "--access", intruder, "--secret", "x", "--role", "userplus",
                      secret="not-the-root-secret")
    assert forged.returncode != 0
    assert gw.users() == before


def test_missing_account_bucket_fails_admin_requests(stopped_gateway):
    backend().create_bucket(Bucket=stopped_gateway.meta)
    stopped_gateway.start()
    created = stopped_gateway.admin("create-user", "--access", f"t-{stopped_gateway.suffix}", "--secret", "s",
                                    "--role", "userplus")
    assert created.returncode != 0
    listed = stopped_gateway.admin("list-users")
    assert listed.returncode != 0
    assert stopped_gateway.iamb not in upstream_buckets()
    assert stopped_gateway.proc.poll() is None


def test_prewritten_account_store_is_used(stopped_gateway):
    gw = stopped_gateway
    s3 = backend()
    s3.create_bucket(Bucket=gw.meta)
    s3.create_bucket(Bucket=gw.iamb)
    a, b, c = f"pre-a-{gw.suffix}", f"pre-b-{gw.suffix}", f"pre-c-{gw.suffix}"
    seeded = {"accessAccounts": {
        a: {"access": a, "secret": "alpha", "role": "userplus"},
        b: {"access": b, "secret": "bravo", "role": "userplus", "userID": 7, "groupID": 8, "projectID": 9},
    }}
    s3.put_object(Bucket=gw.iamb, Key="users.json", Body=json.dumps(seeded).encode())
    gw.start()

    listed = gw.admin("list-users")
    assert listed.returncode == 0, listed.stderr
    rows = [row for row in table_rows(listed.stdout) if any(gw.suffix in cell for cell in row)]
    assert sorted(rows) == sorted([[a, "userplus", "0", "0", "0"], [b, "userplus", "7", "8", "9"]])

    tenant_a, tenant_b = gw.client(a, "alpha"), gw.client(b, "bravo")
    bucket = gw.name("pre")
    tenant_a.create_bucket(Bucket=bucket)
    assert gw.acl_record(bucket)["Owner"] == a
    assert bucket_names(tenant_a) == [bucket]
    assert bucket_names(tenant_b) == []
    assert error_of(lambda: gw.client(a, "bravo").list_buckets()) == (403, "SignatureDoesNotMatch")

    gw.create_user(c, "charlie")
    doc = gw.users()
    assert set(doc) == {"accessAccounts"}
    assert set(doc["accessAccounts"]) == {a, b, c}
    assert_account(doc["accessAccounts"][a], a, "alpha", "userplus", (0, 0, 0))
    assert_account(doc["accessAccounts"][b], b, "bravo", "userplus", (7, 8, 9))
    assert_account(doc["accessAccounts"][c], c, "charlie", "userplus", (0, 0, 0))
    assert bucket_names(gw.client(c, "charlie")) == []
