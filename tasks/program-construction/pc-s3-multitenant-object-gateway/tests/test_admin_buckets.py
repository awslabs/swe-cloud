"""Admin client bucket commands: list-buckets, change-bucket-owner and create-bucket."""

from conftest import bucket_contents, bucket_names, error_of, upstream_buckets


def rows(output, suffix):
    return sorted(line.split() for line in output.splitlines() if suffix in line)


def test_list_buckets_prints_bucket_and_owner(tenants):
    a, b = tenants
    gw = a.gateway
    a1, b1 = gw.name("lb-a"), gw.name("lb-b")
    a.s3.create_bucket(Bucket=a1)
    b.s3.create_bucket(Bucket=b1)
    listed = gw.admin("list-buckets")
    assert listed.returncode == 0, listed.stderr
    found = rows(listed.stdout, gw.suffix)
    assert [a1, a.access] in found
    assert [b1, b.access] in found


def test_list_buckets_omits_metadata_bucket(tenants):
    a, _ = tenants
    gw = a.gateway
    a.s3.create_bucket(Bucket=gw.name("lb"))
    listed = gw.admin("list-buckets")
    assert listed.returncode == 0, listed.stderr
    assert gw.meta not in listed.stdout
    assert rows(listed.stdout, gw.suffix) == sorted([[gw.name("lb"), a.access], [gw.iamb]])


def test_list_buckets_requires_root(tenants):
    a, _ = tenants
    gw = a.gateway
    a.s3.create_bucket(Bucket=gw.name("lb"))
    listed = gw.admin("list-buckets", access=a.access, secret=a.secret)
    assert listed.returncode != 0
    assert gw.name("lb") not in listed.stdout


def test_change_bucket_owner_rewrites_record(tenants):
    a, b = tenants
    gw = a.gateway
    bucket = gw.name("moved")
    a.s3.create_bucket(Bucket=bucket)
    result = gw.admin("change-bucket-owner", "--bucket", bucket, "--owner", b.access)
    assert result.returncode == 0, result.stderr
    assert gw.acl_record(bucket) == {"Owner": b.access, "Grantees": [
        {"Permission": "FULL_CONTROL", "Access": b.access, "Type": "CanonicalUser"}]}


def test_change_bucket_owner_moves_access(tenants):
    a, b = tenants
    gw = a.gateway
    bucket = gw.name("moved")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="handed-over", Body=b"payload")
    assert gw.admin("change-bucket-owner", "--bucket", bucket, "--owner", b.access).returncode == 0
    assert bucket_names(b.s3) == [bucket]
    assert bucket_names(a.s3) == []
    assert b.s3.get_object(Bucket=bucket, Key="handed-over")["Body"].read() == b"payload"
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="handed-over")) == (403, "AccessDenied")


def test_change_bucket_owner_to_unknown_account_fails(tenants):
    a, _ = tenants
    gw = a.gateway
    bucket = gw.name("stays")
    a.s3.create_bucket(Bucket=bucket)
    before = gw.acl_record(bucket)
    result = gw.admin("change-bucket-owner", "--bucket", bucket, "--owner", f"ghost-{gw.suffix}")
    assert result.returncode != 0
    assert gw.acl_record(bucket) == before


def test_change_bucket_owner_requires_root(tenants):
    a, b = tenants
    gw = a.gateway
    bucket = gw.name("guarded")
    a.s3.create_bucket(Bucket=bucket)
    before = gw.acl_record(bucket)
    result = gw.admin("change-bucket-owner", "--bucket", bucket, "--owner", b.access, access=b.access, secret=b.secret)
    assert result.returncode != 0
    assert gw.acl_record(bucket) == before


def test_admin_create_bucket_for_owner(tenants):
    a, b = tenants
    gw = a.gateway
    bucket = gw.name("granted")
    result = gw.admin("create-bucket", "--bucket", bucket, "--owner", b.access)
    assert result.returncode == 0, result.stderr
    assert bucket in upstream_buckets()
    assert gw.acl_record(bucket)["Owner"] == b.access
    assert bucket_names(b.s3) == [bucket]
    assert bucket_names(a.s3) == []


def test_admin_create_bucket_with_taken_name_fails(tenants):
    a, b = tenants
    gw = a.gateway
    bucket = gw.name("taken")
    a.s3.create_bucket(Bucket=bucket)
    meta_before = bucket_contents(gw.meta)
    assert gw.admin("create-bucket", "--bucket", bucket, "--owner", b.access).returncode != 0
    assert bucket_contents(gw.meta) == meta_before
