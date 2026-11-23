"""Admin client account maintenance: update-user, delete-user and the user and admin roles."""

from conftest import backend, bucket_names, error_of, upstream_buckets


def test_update_user_changes_secret(tenants):
    a, _ = tenants
    gw = a.gateway
    result = gw.admin("update-user", "--access", a.access, "--secret", "rotated-secret")
    assert result.returncode == 0, result.stderr
    assert gw.users()["accessAccounts"][a.access]["secret"] == "rotated-secret"
    assert bucket_names(gw.client(a.access, "rotated-secret")) == []
    assert error_of(lambda: a.s3.list_buckets()) == (403, "SignatureDoesNotMatch")


def test_update_user_sets_numeric_ids(tenants):
    a, b = tenants
    gw = a.gateway
    result = gw.admin("update-user", "--access", a.access, "--user-id", "501", "--group-id", "502", "--project-id", "503")
    assert result.returncode == 0, result.stderr
    record = gw.users()["accessAccounts"][a.access]
    assert (record["userID"], record["groupID"], record["projectID"]) == (501, 502, 503)
    assert record["secret"] == a.secret and record["role"] == "userplus"
    assert gw.users()["accessAccounts"][b.access]["userID"] == 0


def test_update_unknown_user_fails(tenants):
    gw = tenants[0].gateway
    before = gw.users()
    result = gw.admin("update-user", "--access", f"ghost-{gw.suffix}", "--secret", "x")
    assert result.returncode != 0
    assert gw.users() == before


def test_delete_user_removes_account(tenants):
    a, b = tenants
    gw = a.gateway
    result = gw.admin("delete-user", "--access", b.access)
    assert result.returncode == 0, result.stderr
    assert set(gw.users()["accessAccounts"]) == {a.access}
    assert error_of(lambda: b.s3.list_buckets()) == (403, "InvalidAccessKeyId")
    assert bucket_names(a.s3) == []


def test_delete_unknown_user_fails(tenants):
    gw = tenants[0].gateway
    before = gw.users()
    assert gw.admin("delete-user", "--access", f"ghost-{gw.suffix}").returncode != 0
    assert gw.users() == before


def test_tenant_cannot_update_accounts(tenants):
    a, b = tenants
    gw = a.gateway
    before = gw.users()
    result = gw.admin("update-user", "--access", b.access, "--secret", "taken-over", access=a.access, secret=a.secret)
    assert result.returncode != 0
    assert gw.users() == before


def test_user_role_account_is_recorded(gateway):
    access = f"plain-{gateway.suffix}"
    result = gateway.admin("create-user", "--access", access, "--secret", "plain-secret", "--role", "user")
    assert result.returncode == 0, result.stderr
    record = gateway.users()["accessAccounts"][access]
    assert record["role"] == "user" and record["secret"] == "plain-secret"


def test_user_role_cannot_create_buckets(gateway):
    access = f"plain-{gateway.suffix}"
    assert gateway.admin("create-user", "--access", access, "--secret", "plain-secret", "--role", "user").returncode == 0
    client = gateway.client(access, "plain-secret")
    bucket = gateway.name("by-user")
    assert error_of(lambda: client.create_bucket(Bucket=bucket)) == (403, "AccessDenied")
    assert bucket not in upstream_buckets()
    assert bucket_names(client) == []


def test_downgraded_user_keeps_its_bucket(tenants):
    a, _ = tenants
    gw = a.gateway
    bucket = gw.name("kept")
    a.s3.create_bucket(Bucket=bucket)
    assert gw.admin("update-user", "--access", a.access, "--role", "user").returncode == 0
    a.s3.put_object(Bucket=bucket, Key="still-mine", Body=b"ok")
    assert bucket_names(a.s3) == [bucket]
    assert backend().get_object(Bucket=bucket, Key="still-mine")["Body"].read() == b"ok"
    assert error_of(lambda: a.s3.create_bucket(Bucket=gw.name("another"))) == (403, "AccessDenied")


def test_admin_role_account_can_use_admin_client(gateway):
    admin, secret = f"ops-{gateway.suffix}", "ops-secret"
    assert gateway.admin("create-user", "--access", admin, "--secret", secret, "--role", "admin").returncode == 0
    tenant = f"made-by-ops-{gateway.suffix}"
    result = gateway.admin("create-user", "--access", tenant, "--secret", "t", "--role", "userplus", access=admin, secret=secret)
    assert result.returncode == 0, result.stderr
    assert set(gateway.users()["accessAccounts"]) == {admin, tenant}
