"""Presigned (query-string SigV4) URLs issued with a tenant's own keys."""

import urllib.error
import urllib.request

import boto3
import pytest
from botocore.config import Config
from conftest import bucket_contents


def presigner(gw, tenant):
    return boto3.client("s3", endpoint_url=gw.url, aws_access_key_id=tenant.access, aws_secret_access_key=tenant.secret,
                        region_name="us-east-1", config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))


def fetch(url, data=None, method="GET"):
    """(status, body) of a plain HTTP request."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture
def owned(tenants):
    a, b = tenants
    bucket = a.gateway.name("presign")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="shared.txt", Body=b"presigned content")
    return a, b, bucket


def test_presigned_get_returns_object(owned):
    a, _, bucket = owned
    url = presigner(a.gateway, a).generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "shared.txt"}, ExpiresIn=600)
    assert fetch(url) == (200, b"presigned content")


def test_presigned_put_stores_object(owned):
    a, _, bucket = owned
    url = presigner(a.gateway, a).generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": "uploaded"}, ExpiresIn=600)
    status, _ = fetch(url, data=b"sent by url", method="PUT")
    assert status == 200
    assert bucket_contents(bucket) == {"shared.txt": b"presigned content", "uploaded": b"sent by url"}


def test_tampered_presigned_url_is_rejected(owned):
    a, _, bucket = owned
    url = presigner(a.gateway, a).generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "shared.txt"}, ExpiresIn=600)
    head, signature = url.rsplit("X-Amz-Signature=", 1)
    forged = head + "X-Amz-Signature=" + ("0" if signature[0] != "0" else "1") + signature[1:]
    status, body = fetch(forged)
    assert status == 403
    assert b"<Code>SignatureDoesNotMatch</Code>" in body


def test_presigned_url_of_other_tenant_is_denied(owned):
    a, b, bucket = owned
    url = presigner(a.gateway, b).generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "shared.txt"}, ExpiresIn=600)
    status, body = fetch(url)
    assert status == 403
    assert b"<Code>AccessDenied</Code>" in body
