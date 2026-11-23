"""DeleteBucket by the owner, for non-empty buckets and by other tenants."""

from conftest import bucket_contents, bucket_names, error_of, upstream_buckets


def test_owner_deletes_empty_bucket(tenants):
    a, _ = tenants
    gw = a.gateway
    keep, gone = gw.name("keep"), gw.name("gone")
    a.s3.create_bucket(Bucket=keep)
    a.s3.create_bucket(Bucket=gone)
    assert a.s3.delete_bucket(Bucket=gone)["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert gone not in upstream_buckets()
    assert bucket_names(a.s3) == [keep]


def test_deleting_non_empty_bucket_conflicts(tenants):
    a, _ = tenants
    bucket = a.gateway.name("full")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="still-here", Body=b"x")
    assert error_of(lambda: a.s3.delete_bucket(Bucket=bucket)) == (409, "BucketNotEmpty")
    assert bucket_contents(bucket) == {"still-here": b"x"}
    assert bucket_names(a.s3) == [bucket]


def test_other_tenant_cannot_delete_bucket(tenants):
    a, b = tenants
    bucket = a.gateway.name("mine")
    a.s3.create_bucket(Bucket=bucket)
    assert error_of(lambda: b.s3.delete_bucket(Bucket=bucket)) == (403, "AccessDenied")
    assert bucket in upstream_buckets()
    assert bucket_names(a.s3) == [bucket]


def test_metadata_bucket_cannot_be_deleted(tenants):
    a, _ = tenants
    gw = a.gateway
    assert error_of(lambda: a.s3.delete_bucket(Bucket=gw.meta))[0] == 403
    assert gw.meta in upstream_buckets()
