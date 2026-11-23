"""Object headers beyond type and user metadata: standard headers, response overrides, checksums, attributes."""

import base64
import hashlib

import pytest
from conftest import backend, bucket_contents, error_of


@pytest.fixture
def owned(tenants):
    a, _ = tenants
    bucket = a.gateway.name("hdrs")
    a.s3.create_bucket(Bucket=bucket)
    return a, bucket


def put_with_headers(a, bucket):
    a.s3.put_object(Bucket=bucket, Key="page.html", Body=b"<p>hi</p>", ContentType="text/html",
                    CacheControl="max-age=60", ContentDisposition='attachment; filename="page.html"',
                    ContentEncoding="identity")


def test_standard_headers_stored_upstream(owned):
    a, bucket = owned
    put_with_headers(a, bucket)
    upstream = backend().head_object(Bucket=bucket, Key="page.html")
    assert upstream["CacheControl"] == "max-age=60"
    assert upstream["ContentDisposition"] == 'attachment; filename="page.html"'
    assert upstream["ContentEncoding"] == "identity"


def test_get_returns_standard_headers(owned):
    a, bucket = owned
    put_with_headers(a, bucket)
    got = a.s3.get_object(Bucket=bucket, Key="page.html")
    assert got["CacheControl"] == "max-age=60"
    assert got["ContentDisposition"] == 'attachment; filename="page.html"'
    assert got["ContentEncoding"] == "identity"


def test_get_response_overrides(owned):
    a, bucket = owned
    put_with_headers(a, bucket)
    got = a.s3.get_object(Bucket=bucket, Key="page.html", ResponseContentType="text/plain",
                          ResponseContentDisposition="inline", ResponseCacheControl="no-store")
    assert got["ContentType"] == "text/plain"
    assert got["ContentDisposition"] == "inline"
    assert got["CacheControl"] == "no-store"
    assert backend().head_object(Bucket=bucket, Key="page.html")["ContentType"] == "text/html"


def sha256_b64(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_put_with_sha256_checksum_returns_it(owned):
    a, bucket = owned
    put = a.s3.put_object(Bucket=bucket, Key="sum", Body=b"checksummed", ChecksumAlgorithm="SHA256")
    assert put["ChecksumSHA256"] == sha256_b64(b"checksummed")


def test_sha256_checksum_stored_upstream(owned):
    a, bucket = owned
    a.s3.put_object(Bucket=bucket, Key="sum", Body=b"checksummed", ChecksumAlgorithm="SHA256")
    upstream = backend().head_object(Bucket=bucket, Key="sum", ChecksumMode="ENABLED")
    assert upstream["ChecksumSHA256"] == sha256_b64(b"checksummed")
    assert a.s3.head_object(Bucket=bucket, Key="sum", ChecksumMode="ENABLED")["ChecksumSHA256"] == sha256_b64(b"checksummed")


def test_wrong_sha256_checksum_is_bad_digest(owned):
    a, bucket = owned
    assert error_of(lambda: a.s3.put_object(
        Bucket=bucket, Key="sum", Body=b"checksummed", ChecksumSHA256=sha256_b64(b"something else"))) == (400, "BadDigest")
    assert bucket_contents(bucket) == {}


def test_get_object_attributes(owned):
    a, bucket = owned
    a.s3.put_object(Bucket=bucket, Key="attrs", Body=b"x" * 321)
    attrs = a.s3.get_object_attributes(Bucket=bucket, Key="attrs", ObjectAttributes=["ETag", "ObjectSize"])
    assert attrs["ObjectSize"] == 321
    assert attrs["ETag"] == hashlib.md5(b"x" * 321).hexdigest()


def test_get_object_attributes_of_missing_key(owned):
    a, bucket = owned
    assert error_of(lambda: a.s3.get_object_attributes(
        Bucket=bucket, Key="absent", ObjectAttributes=["ETag"])) == (404, "NoSuchKey")
