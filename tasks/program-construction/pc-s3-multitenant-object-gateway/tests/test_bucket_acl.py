"""Bucket ACLs: reading and replacing grants, canned ACLs, and the ownership-control interplay."""

import pytest
from conftest import backend, bucket_contents, error_of, upstream_buckets


@pytest.fixture
def owned(tenants):
    a, b = tenants
    bucket = a.gateway.name("acl")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="item", Body=b"acl item")
    return a, b, bucket


def grants(response):
    return sorted((g["Grantee"].get("ID") or g["Grantee"].get("URI"), g["Grantee"]["Type"], g["Permission"])
                  for g in response["Grants"])


def grant_read(a, b, bucket):
    a.s3.put_bucket_acl(Bucket=bucket, AccessControlPolicy={
        "Owner": {"ID": a.access},
        "Grants": [{"Grantee": {"ID": b.access, "Type": "CanonicalUser"}, "Permission": "READ"}]})


def test_get_bucket_acl_shows_owner_full_control(owned):
    a, _, bucket = owned
    acl = a.s3.get_bucket_acl(Bucket=bucket)
    assert acl["Owner"]["ID"] == a.access
    assert grants(acl) == [(a.access, "CanonicalUser", "FULL_CONTROL")]


def test_put_bucket_acl_rewrites_record(owned):
    a, b, bucket = owned
    grant_read(a, b, bucket)
    record = a.gateway.acl_record(bucket)
    assert record["Owner"] == a.access
    assert sorted((g["Access"], g["Type"], g["Permission"]) for g in record["Grantees"]) == sorted([
        (a.access, "CanonicalUser", "FULL_CONTROL"), (b.access, "CanonicalUser", "READ")])


def test_get_bucket_acl_after_grant(owned):
    a, b, bucket = owned
    grant_read(a, b, bucket)
    assert grants(a.s3.get_bucket_acl(Bucket=bucket)) == sorted([
        (a.access, "CanonicalUser", "FULL_CONTROL"), (b.access, "CanonicalUser", "READ")])


def test_read_grant_lets_grantee_list_and_get(owned):
    a, b, bucket = owned
    grant_read(a, b, bucket)
    assert [o["Key"] for o in b.s3.list_objects_v2(Bucket=bucket)["Contents"]] == ["item"]
    assert b.s3.get_object(Bucket=bucket, Key="item")["Body"].read() == b"acl item"


def test_read_grant_does_not_allow_writes(owned):
    a, b, bucket = owned
    grant_read(a, b, bucket)
    assert error_of(lambda: b.s3.put_object(Bucket=bucket, Key="item", Body=b"x")) == (403, "AccessDenied")
    assert error_of(lambda: b.s3.delete_object(Bucket=bucket, Key="item")) == (403, "AccessDenied")
    assert bucket_contents(bucket) == {"item": b"acl item"}


def test_canned_public_read_records_all_users_group(owned):
    a, _, bucket = owned
    a.s3.put_bucket_acl(Bucket=bucket, ACL="public-read")
    record = a.gateway.acl_record(bucket)
    assert sorted((g["Access"], g["Type"], g["Permission"]) for g in record["Grantees"]) == sorted([
        (a.access, "CanonicalUser", "FULL_CONTROL"), ("all-users", "Group", "READ")])


def test_public_read_acl_allows_anonymous_reads(owned):
    a, _, bucket = owned
    a.s3.put_bucket_acl(Bucket=bucket, ACL="public-read")
    anon = a.gateway.anonymous()
    assert [o["Key"] for o in anon.list_objects_v2(Bucket=bucket)["Contents"]] == ["item"]
    assert anon.get_object(Bucket=bucket, Key="item")["Body"].read() == b"acl item"
    assert error_of(lambda: anon.put_object(Bucket=bucket, Key="anon", Body=b"x")) == (403, "AccessDenied")


def test_private_canned_acl_restores_owner_only(owned):
    a, b, bucket = owned
    grant_read(a, b, bucket)
    a.s3.put_bucket_acl(Bucket=bucket, ACL="private")
    assert a.gateway.acl_record(bucket)["Grantees"] == [
        {"Permission": "FULL_CONTROL", "Access": a.access, "Type": "CanonicalUser"}]
    assert error_of(lambda: b.s3.get_object(Bucket=bucket, Key="item")) == (403, "AccessDenied")


def test_acl_refused_when_bucket_owner_enforced(owned):
    a, _, bucket = owned
    a.s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]})
    before = a.gateway.acl_record(bucket)
    assert error_of(lambda: a.s3.put_bucket_acl(Bucket=bucket, ACL="public-read")) == (400, "AccessControlListNotSupported")
    assert a.gateway.acl_record(bucket) == before


def test_create_bucket_with_canned_acl_and_object_writer(tenants):
    a, _ = tenants
    bucket = a.gateway.name("acl-pub")
    a.s3.create_bucket(Bucket=bucket, ACL="public-read", ObjectOwnership="ObjectWriter")
    record = a.gateway.acl_record(bucket)
    assert record["Owner"] == a.access
    assert sorted((g["Access"], g["Type"], g["Permission"]) for g in record["Grantees"]) == sorted([
        (a.access, "CanonicalUser", "FULL_CONTROL"), ("all-users", "Group", "READ")])


def test_create_bucket_with_acl_under_default_ownership_is_refused(tenants):
    a, _ = tenants
    bucket = a.gateway.name("acl-enf")
    assert error_of(lambda: a.s3.create_bucket(Bucket=bucket, ACL="public-read")) == (400, "InvalidBucketAclWithObjectOwnership")
    assert bucket not in upstream_buckets()
    assert bucket_contents(a.gateway.meta) == {}
