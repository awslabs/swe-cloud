"""CORS and website configurations, stored as records in the metadata bucket."""

import pytest
from conftest import bucket_contents, error_of

CORS_PREFIX = "vgw-meta-cors-"
WEBSITE_PREFIX = "vgw-meta-website-"
RULE = {"AllowedMethods": ["GET", "PUT"], "AllowedOrigins": ["https://app.example.test"],
        "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 300}


@pytest.fixture
def owned(tenants):
    a, b = tenants
    bucket = a.gateway.name("conf")
    a.s3.create_bucket(Bucket=bucket)
    return a, b, bucket


def meta_keys(gw):
    return set(bucket_contents(gw.meta))


def test_put_cors_stores_record(owned):
    a, _, bucket = owned
    a.s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [RULE]})
    assert meta_keys(a.gateway) == {"vgw-meta-acl-" + bucket, CORS_PREFIX + bucket}
    record = bucket_contents(a.gateway.meta)[CORS_PREFIX + bucket]
    assert b"https://app.example.test" in record and b"<AllowedMethod>PUT</AllowedMethod>" in record


def test_get_cors_returns_rules(owned):
    a, _, bucket = owned
    a.s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [RULE]})
    rules = a.s3.get_bucket_cors(Bucket=bucket)["CORSRules"]
    assert len(rules) == 1
    assert {k: rules[0][k] for k in RULE} == RULE


def test_get_cors_when_none_is_set(owned):
    a, _, bucket = owned
    assert error_of(lambda: a.s3.get_bucket_cors(Bucket=bucket)) == (404, "NoSuchCORSConfiguration")


def test_delete_cors_removes_record(owned):
    a, _, bucket = owned
    a.s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [RULE]})
    assert a.s3.delete_bucket_cors(Bucket=bucket)["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert meta_keys(a.gateway) == {"vgw-meta-acl-" + bucket}


def test_cors_with_unsupported_method_is_rejected(owned):
    a, _, bucket = owned
    bad = {"CORSRules": [{"AllowedMethods": ["BREW"], "AllowedOrigins": ["*"]}]}
    assert error_of(lambda: a.s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=bad)) == (400, "InvalidRequest")
    assert meta_keys(a.gateway) == {"vgw-meta-acl-" + bucket}


def test_put_website_stores_record(owned):
    a, _, bucket = owned
    a.s3.put_bucket_website(Bucket=bucket, WebsiteConfiguration={
        "IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "error.html"}})
    assert meta_keys(a.gateway) == {"vgw-meta-acl-" + bucket, WEBSITE_PREFIX + bucket}
    record = bucket_contents(a.gateway.meta)[WEBSITE_PREFIX + bucket]
    assert b"index.html" in record and b"error.html" in record


def test_get_website_returns_configuration(owned):
    a, _, bucket = owned
    a.s3.put_bucket_website(Bucket=bucket, WebsiteConfiguration={
        "IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "error.html"}})
    site = a.s3.get_bucket_website(Bucket=bucket)
    assert site["IndexDocument"] == {"Suffix": "index.html"}
    assert site["ErrorDocument"] == {"Key": "error.html"}


def test_get_website_when_none_is_set(owned):
    a, _, bucket = owned
    assert error_of(lambda: a.s3.get_bucket_website(Bucket=bucket)) == (404, "NoSuchWebsiteConfiguration")


def test_delete_website_removes_record(owned):
    a, _, bucket = owned
    a.s3.put_bucket_website(Bucket=bucket, WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
    assert a.s3.delete_bucket_website(Bucket=bucket)["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert meta_keys(a.gateway) == {"vgw-meta-acl-" + bucket}
    assert error_of(lambda: a.s3.get_bucket_website(Bucket=bucket)) == (404, "NoSuchWebsiteConfiguration")


def test_other_tenant_cannot_set_cors(owned):
    a, b, bucket = owned
    assert error_of(lambda: b.s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [RULE]})) == (403, "AccessDenied")
    assert meta_keys(a.gateway) == {"vgw-meta-acl-" + bucket}
