"""CopyObject between keys and buckets the tenant owns, and copies that must be refused."""

import pytest
from conftest import backend, bucket_contents, error_of

BODY = b"copy me \x00\x01\x02 please\n"


@pytest.fixture
def source(tenants):
    a, b = tenants
    bucket = a.gateway.name("copysrc")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="orig", Body=BODY, ContentType="text/x-orig", Metadata={"tone": "blue"})
    return a, b, bucket


def test_copy_within_bucket_copies_bytes(source):
    a, _, bucket = source
    a.s3.copy_object(Bucket=bucket, Key="dup", CopySource={"Bucket": bucket, "Key": "orig"})
    assert bucket_contents(bucket) == {"orig": BODY, "dup": BODY}


def test_copy_keeps_type_and_metadata_by_default(source):
    a, _, bucket = source
    a.s3.copy_object(Bucket=bucket, Key="dup", CopySource={"Bucket": bucket, "Key": "orig"})
    upstream = backend().head_object(Bucket=bucket, Key="dup")
    assert upstream["ContentType"] == "text/x-orig"
    assert upstream["Metadata"] == {"tone": "blue"}


def test_copy_with_replace_directive_sets_new_metadata(source):
    a, _, bucket = source
    a.s3.copy_object(Bucket=bucket, Key="dup", CopySource={"Bucket": bucket, "Key": "orig"},
                     MetadataDirective="REPLACE", ContentType="application/x-new", Metadata={"tone": "red", "extra": "1"})
    upstream = backend().head_object(Bucket=bucket, Key="dup")
    assert upstream["ContentType"] == "application/x-new"
    assert upstream["Metadata"] == {"tone": "red", "extra": "1"}
    assert bucket_contents(bucket)["dup"] == BODY


def test_copy_returns_upstream_etag(source):
    a, _, bucket = source
    copied = a.s3.copy_object(Bucket=bucket, Key="dup", CopySource={"Bucket": bucket, "Key": "orig"})
    assert copied["CopyObjectResult"]["ETag"] == backend().head_object(Bucket=bucket, Key="dup")["ETag"]


def test_copy_between_own_buckets(source):
    a, _, bucket = source
    other = a.gateway.name("copydst")
    a.s3.create_bucket(Bucket=other)
    a.s3.copy_object(Bucket=other, Key="moved/here", CopySource={"Bucket": bucket, "Key": "orig"})
    assert bucket_contents(other) == {"moved/here": BODY}
    assert bucket_contents(bucket) == {"orig": BODY}


def test_copy_of_missing_source_key(source):
    a, _, bucket = source
    assert error_of(lambda: a.s3.copy_object(
        Bucket=bucket, Key="dup", CopySource={"Bucket": bucket, "Key": "absent"})) == (404, "NoSuchKey")
    assert bucket_contents(bucket) == {"orig": BODY}


def test_copy_source_if_match_mismatch_fails(source):
    a, _, bucket = source
    assert error_of(lambda: a.s3.copy_object(
        Bucket=bucket, Key="dup", CopySource={"Bucket": bucket, "Key": "orig"},
        CopySourceIfMatch='"00000000000000000000000000000000"')) == (412, "PreconditionFailed")
    assert bucket_contents(bucket) == {"orig": BODY}


def test_copy_from_foreign_bucket_denied(source):
    a, b, bucket = source
    mine = a.gateway.name("copy-b")
    b.s3.create_bucket(Bucket=mine)
    assert error_of(lambda: b.s3.copy_object(
        Bucket=mine, Key="stolen", CopySource={"Bucket": bucket, "Key": "orig"})) == (403, "AccessDenied")
    assert bucket_contents(mine) == {}
