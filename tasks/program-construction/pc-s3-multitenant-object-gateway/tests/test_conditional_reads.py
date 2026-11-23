"""Range reads and conditional requests (If-Match / If-None-Match) on GET, HEAD and PUT."""

import pytest
from conftest import bucket_contents, error_of

BODY = b"0123456789abcdefghijklmnopqrstuvwxyz"
OTHER_ETAG = '"00000000000000000000000000000000"'


@pytest.fixture
def stored(tenants):
    a, _ = tenants
    bucket = a.gateway.name("cond")
    a.s3.create_bucket(Bucket=bucket)
    etag = a.s3.put_object(Bucket=bucket, Key="obj", Body=BODY)["ETag"]
    return a, bucket, etag


def test_range_get_returns_partial_content(stored):
    a, bucket, _ = stored
    got = a.s3.get_object(Bucket=bucket, Key="obj", Range="bytes=10-15")
    assert got["ResponseMetadata"]["HTTPStatusCode"] == 206
    assert got["Body"].read() == BODY[10:16]
    assert got["ContentRange"] == f"bytes 10-15/{len(BODY)}"
    assert got["ContentLength"] == 6


def test_suffix_range_get_returns_last_bytes(stored):
    a, bucket, _ = stored
    got = a.s3.get_object(Bucket=bucket, Key="obj", Range="bytes=-4")
    assert got["ResponseMetadata"]["HTTPStatusCode"] == 206
    assert got["Body"].read() == BODY[-4:]


def test_unsatisfiable_range_is_rejected(stored):
    a, bucket, _ = stored
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="obj", Range="bytes=500-600")) == (416, "InvalidRange")


def test_get_if_match_current_etag_returns_object(stored):
    a, bucket, etag = stored
    assert a.s3.get_object(Bucket=bucket, Key="obj", IfMatch=etag)["Body"].read() == BODY


def test_get_if_match_other_etag_fails(stored):
    a, bucket, _ = stored
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="obj", IfMatch=OTHER_ETAG)) == (412, "PreconditionFailed")


def test_get_if_none_match_current_etag_not_modified(stored):
    a, bucket, etag = stored
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="obj", IfNoneMatch=etag))[0] == 304


def test_head_if_none_match_current_etag_not_modified(stored):
    a, bucket, etag = stored
    assert error_of(lambda: a.s3.head_object(Bucket=bucket, Key="obj", IfNoneMatch=etag))[0] == 304


def test_put_if_none_match_star_refuses_existing_key(stored):
    a, bucket, _ = stored
    assert error_of(lambda: a.s3.put_object(Bucket=bucket, Key="obj", Body=b"overwrite", IfNoneMatch="*")) == (
        412, "PreconditionFailed")
    assert bucket_contents(bucket) == {"obj": BODY}


def test_put_if_none_match_star_creates_new_key(stored):
    a, bucket, _ = stored
    a.s3.put_object(Bucket=bucket, Key="fresh", Body=b"new", IfNoneMatch="*")
    assert bucket_contents(bucket) == {"obj": BODY, "fresh": b"new"}
