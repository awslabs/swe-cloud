"""Bucket creation, ownership records, HeadBucket and per-tenant ListBuckets."""

from conftest import ACL_PREFIX, backend, bucket_contents, bucket_names, error_of, upstream_buckets


def expected_acl(owner):
    return {"Owner": owner, "Grantees": [{"Permission": "FULL_CONTROL", "Access": owner, "Type": "CanonicalUser"}]}


def test_create_bucket_records_owner(tenants):
    a, _ = tenants
    gw = a.gateway
    bucket = gw.name("own")
    a.s3.create_bucket(Bucket=bucket)
    assert bucket in upstream_buckets()
    assert bucket_contents(bucket) == {}
    assert set(bucket_contents(gw.meta)) == {ACL_PREFIX + bucket}
    assert gw.acl_record(bucket) == expected_acl(a.access)
    # Creating a bucket does not touch the account store.
    assert set(gw.users()["accessAccounts"]) == {a.access, tenants[1].access}


def test_recreate_existing_bucket_conflicts(tenants):
    a, b = tenants
    gw = a.gateway
    bucket = gw.name("taken")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="keep.txt", Body=b"kept by a")
    meta_before = bucket_contents(gw.meta)

    assert error_of(lambda: a.s3.create_bucket(Bucket=bucket)) == (409, "BucketAlreadyOwnedByYou")
    assert bucket_contents(gw.meta) == meta_before
    assert bucket_contents(bucket) == {"keep.txt": b"kept by a"}

    assert error_of(lambda: b.s3.create_bucket(Bucket=bucket)) == (409, "BucketAlreadyExists")
    assert bucket_contents(gw.meta) == meta_before
    assert gw.acl_record(bucket) == expected_acl(a.access)
    assert bucket_contents(bucket) == {"keep.txt": b"kept by a"}
    assert bucket_names(b.s3) == []
    assert bucket_names(a.s3) == [bucket]


def test_metadata_bucket_name_cannot_be_created(tenants):
    a, _ = tenants
    gw = a.gateway
    own = gw.name("mine")
    a.s3.create_bucket(Bucket=own)
    meta_before = bucket_contents(gw.meta)
    assert error_of(lambda: a.s3.create_bucket(Bucket=gw.meta)) == (409, "BucketAlreadyExists")
    assert bucket_contents(gw.meta) == meta_before
    assert ACL_PREFIX + gw.meta not in meta_before
    assert bucket_names(a.s3) == [own]


def test_invalid_bucket_name_rejected(tenants):
    a, _ = tenants
    gw = a.gateway
    assert error_of(lambda: a.s3.create_bucket(Bucket="Bad_Name")) == (400, "InvalidBucketName")
    assert bucket_contents(gw.meta) == {}
    assert bucket_names(a.s3) == []


def test_list_buckets_shows_only_owned_buckets(tenants):
    a, b = tenants
    gw = a.gateway
    third = gw.create_user(f"tenantc-{gw.suffix}", "secret-charlie-3")
    raw = gw.name("raw")  # exists upstream, never registered through the gateway
    s3 = backend()
    s3.create_bucket(Bucket=raw)
    s3.put_object(Bucket=raw, Key="k", Body=b"raw")
    a1, a2, b1 = gw.name("a-one"), gw.name("a-two"), gw.name("b-one")
    a.s3.create_bucket(Bucket=a1)
    b.s3.create_bucket(Bucket=b1)
    a.s3.create_bucket(Bucket=a2)

    assert bucket_names(a.s3) == sorted([a1, a2])
    assert bucket_names(b.s3) == [b1]
    assert bucket_names(third.s3) == []
    assert {a1, a2, b1, raw, gw.meta, gw.iamb} <= upstream_buckets()
    assert set(bucket_contents(gw.meta)) == {ACL_PREFIX + a1, ACL_PREFIX + a2, ACL_PREFIX + b1}
    assert gw.acl_record(a2) == expected_acl(a.access)
    assert gw.acl_record(b1) == expected_acl(b.access)


def test_head_bucket_by_owner_only(tenants):
    a, b = tenants
    gw = a.gateway
    mine, theirs = gw.name("head-a"), gw.name("head-b")
    raw = gw.name("head-raw")
    a.s3.create_bucket(Bucket=mine)
    b.s3.create_bucket(Bucket=theirs)
    backend().create_bucket(Bucket=raw)

    assert a.s3.head_bucket(Bucket=mine)["ResponseMetadata"]["HTTPStatusCode"] == 200
    assert b.s3.head_bucket(Bucket=theirs)["ResponseMetadata"]["HTTPStatusCode"] == 200
    for client, bucket in ((a.s3, theirs), (b.s3, mine), (a.s3, raw), (a.s3, gw.meta), (a.s3, gw.name("absent"))):
        status, _ = error_of(lambda: client.head_bucket(Bucket=bucket))
        assert status == 403, bucket


def test_unregistered_upstream_bucket_is_denied(tenants):
    a, _ = tenants
    gw = a.gateway
    raw = gw.name("orphan")
    s3 = backend()
    s3.create_bucket(Bucket=raw)
    s3.put_object(Bucket=raw, Key="data.txt", Body=b"orphan bytes")
    calls = {
        "get": lambda: a.s3.get_object(Bucket=raw, Key="data.txt"),
        "put": lambda: a.s3.put_object(Bucket=raw, Key="new.txt", Body=b"x"),
        "delete": lambda: a.s3.delete_object(Bucket=raw, Key="data.txt"),
        "list": lambda: a.s3.list_objects_v2(Bucket=raw),
    }
    for label, call in calls.items():
        assert error_of(call) == (403, "AccessDenied"), label
        assert bucket_contents(raw) == {"data.txt": b"orphan bytes"}, label
    assert error_of(lambda: a.s3.head_object(Bucket=raw, Key="data.txt"))[0] == 403
    assert bucket_contents(gw.meta) == {}
    assert bucket_names(a.s3) == []


def test_requests_for_missing_bucket_are_denied(tenants):
    a, _ = tenants
    gw = a.gateway
    missing = gw.name("missing")
    for label, call in {
        "get": lambda: a.s3.get_object(Bucket=missing, Key="k"),
        "put": lambda: a.s3.put_object(Bucket=missing, Key="k", Body=b"1"),
        "list": lambda: a.s3.list_objects_v2(Bucket=missing),
    }.items():
        assert error_of(call) == (403, "AccessDenied"), label
    assert missing not in upstream_buckets()
    assert bucket_contents(gw.meta) == {}
