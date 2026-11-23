"""Bucket policies: the stored record, validation, and the access they grant or deny."""

import json

import pytest
from conftest import bucket_contents, bucket_names, error_of

POLICY_PREFIX = "vgw-meta-policy-"


@pytest.fixture
def shared(tenants):
    a, b = tenants
    bucket = a.gateway.name("pol")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="docs/readme.txt", Body=b"readable")
    return a, b, bucket


def policy(bucket, *statements):
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": effect, "Principal": principal, "Action": action, "Resource": f"arn:aws:s3:::{bucket}{resource}"}
        for effect, principal, action, resource in statements]})


def test_put_policy_stores_record_in_metadata_bucket(shared):
    a, b, bucket = shared
    document = policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*"))
    assert a.s3.put_bucket_policy(Bucket=bucket, Policy=document)["ResponseMetadata"]["HTTPStatusCode"] == 204
    stored = bucket_contents(a.gateway.meta)
    assert set(stored) == {"vgw-meta-acl-" + bucket, POLICY_PREFIX + bucket}
    assert json.loads(stored[POLICY_PREFIX + bucket]) == json.loads(document)


def test_get_policy_returns_document(shared):
    a, b, bucket = shared
    document = policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*"))
    a.s3.put_bucket_policy(Bucket=bucket, Policy=document)
    assert json.loads(a.s3.get_bucket_policy(Bucket=bucket)["Policy"]) == json.loads(document)


def test_get_policy_when_none_is_set(shared):
    a, _, bucket = shared
    assert error_of(lambda: a.s3.get_bucket_policy(Bucket=bucket)) == (404, "NoSuchBucketPolicy")


def test_delete_policy_removes_record(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*")))
    assert a.s3.delete_bucket_policy(Bucket=bucket)["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert set(bucket_contents(a.gateway.meta)) == {"vgw-meta-acl-" + bucket}
    assert error_of(lambda: a.s3.get_bucket_policy(Bucket=bucket)) == (404, "NoSuchBucketPolicy")


def test_policy_that_is_not_json_is_rejected(shared):
    a, _, bucket = shared
    assert error_of(lambda: a.s3.put_bucket_policy(Bucket=bucket, Policy="{not json")) == (400, "MalformedPolicy")
    assert set(bucket_contents(a.gateway.meta)) == {"vgw-meta-acl-" + bucket}


def test_policy_naming_unknown_account_is_rejected(shared):
    a, _, bucket = shared
    document = policy(bucket, ("Allow", {"AWS": ["no-such-account"]}, ["s3:GetObject"], "/*"))
    assert error_of(lambda: a.s3.put_bucket_policy(Bucket=bucket, Policy=document)) == (400, "MalformedPolicy")
    assert set(bucket_contents(a.gateway.meta)) == {"vgw-meta-acl-" + bucket}


def test_policy_for_another_bucket_resource_is_rejected(shared):
    a, b, bucket = shared
    document = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": [b.access]}, "Action": ["s3:GetObject"],
        "Resource": ["arn:aws:s3:::some-other-bucket/*"]}]})
    assert error_of(lambda: a.s3.put_bucket_policy(Bucket=bucket, Policy=document)) == (400, "MalformedPolicy")


def test_get_object_grant_lets_other_tenant_read(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*")))
    assert b.s3.get_object(Bucket=bucket, Key="docs/readme.txt")["Body"].read() == b"readable"


def test_get_object_grant_does_not_allow_writes(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*")))
    assert error_of(lambda: b.s3.put_object(Bucket=bucket, Key="docs/readme.txt", Body=b"x")) == (403, "AccessDenied")
    assert error_of(lambda: b.s3.list_objects_v2(Bucket=bucket)) == (403, "AccessDenied")
    assert bucket_contents(bucket) == {"docs/readme.txt": b"readable"}


def test_list_bucket_grant_allows_listing_only(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:ListBucket"], "")))
    assert [o["Key"] for o in b.s3.list_objects_v2(Bucket=bucket)["Contents"]] == ["docs/readme.txt"]
    assert error_of(lambda: b.s3.get_object(Bucket=bucket, Key="docs/readme.txt")) == (403, "AccessDenied")


def test_put_object_grant_limited_to_prefix(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:PutObject"], "/inbox/*")))
    b.s3.put_object(Bucket=bucket, Key="inbox/from-b.txt", Body=b"delivered")
    assert error_of(lambda: b.s3.put_object(Bucket=bucket, Key="docs/from-b.txt", Body=b"x")) == (403, "AccessDenied")
    assert bucket_contents(bucket) == {"docs/readme.txt": b"readable", "inbox/from-b.txt": b"delivered"}


def test_policy_grant_does_not_list_bucket_for_grantee(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*")))
    assert bucket_names(b.s3) == []
    assert bucket_names(a.s3) == [bucket]


def test_public_policy_allows_anonymous_get(shared):
    a, _, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", "*", "s3:GetObject", "/*")))
    anon = a.gateway.anonymous()
    assert anon.get_object(Bucket=bucket, Key="docs/readme.txt")["Body"].read() == b"readable"
    assert error_of(lambda: anon.list_objects_v2(Bucket=bucket)) == (403, "AccessDenied")


def test_policy_status_reports_public_policy(shared):
    a, b, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", "*", "s3:GetObject", "/*")))
    assert a.s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]["IsPublic"] is True
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*")))
    assert a.s3.get_bucket_policy_status(Bucket=bucket)["PolicyStatus"]["IsPublic"] is False


def test_explicit_deny_applies_to_owner(shared):
    a, _, bucket = shared
    a.s3.put_bucket_policy(Bucket=bucket, Policy=policy(bucket, ("Deny", {"AWS": [a.access]}, ["s3:DeleteObject"], "/*")))
    assert error_of(lambda: a.s3.delete_object(Bucket=bucket, Key="docs/readme.txt")) == (403, "AccessDenied")
    assert bucket_contents(bucket) == {"docs/readme.txt": b"readable"}
    assert a.s3.get_object(Bucket=bucket, Key="docs/readme.txt")["Body"].read() == b"readable"


def test_other_tenant_cannot_set_policy(shared):
    a, b, bucket = shared
    document = policy(bucket, ("Allow", {"AWS": [b.access]}, ["s3:GetObject"], "/*"))
    assert error_of(lambda: b.s3.put_bucket_policy(Bucket=bucket, Policy=document)) == (403, "AccessDenied")
    assert set(bucket_contents(a.gateway.meta)) == {"vgw-meta-acl-" + bucket}
