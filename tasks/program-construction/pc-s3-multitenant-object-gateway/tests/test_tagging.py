"""Object tagging, stored on the upstream objects, and bucket tagging."""

import pytest
from conftest import backend, error_of


@pytest.fixture
def owned(tenants):
    a, _ = tenants
    bucket = a.gateway.name("tags")
    a.s3.create_bucket(Bucket=bucket)
    a.s3.put_object(Bucket=bucket, Key="obj", Body=b"tagged")
    return a, bucket


def tag_map(response):
    return {t["Key"]: t["Value"] for t in response["TagSet"]}


def test_put_tagging_stored_upstream(owned):
    a, bucket = owned
    a.s3.put_object_tagging(Bucket=bucket, Key="obj", Tagging={"TagSet": [
        {"Key": "project", "Value": "apollo"}, {"Key": "tier", "Value": "gold"}]})
    assert tag_map(backend().get_object_tagging(Bucket=bucket, Key="obj")) == {"project": "apollo", "tier": "gold"}


def test_get_tagging_returns_tags(owned):
    a, bucket = owned
    a.s3.put_object_tagging(Bucket=bucket, Key="obj", Tagging={"TagSet": [{"Key": "project", "Value": "apollo"}]})
    assert tag_map(a.s3.get_object_tagging(Bucket=bucket, Key="obj")) == {"project": "apollo"}


def test_put_tagging_replaces_tag_set(owned):
    a, bucket = owned
    a.s3.put_object_tagging(Bucket=bucket, Key="obj", Tagging={"TagSet": [{"Key": "old", "Value": "1"}]})
    a.s3.put_object_tagging(Bucket=bucket, Key="obj", Tagging={"TagSet": [{"Key": "new", "Value": "2"}]})
    assert tag_map(backend().get_object_tagging(Bucket=bucket, Key="obj")) == {"new": "2"}


def test_tagging_header_on_put_object(owned):
    a, bucket = owned
    a.s3.put_object(Bucket=bucket, Key="with-tags", Body=b"x", Tagging="color=red&size=large")
    assert tag_map(backend().get_object_tagging(Bucket=bucket, Key="with-tags")) == {"color": "red", "size": "large"}


def test_delete_tagging_clears_tags(owned):
    a, bucket = owned
    a.s3.put_object_tagging(Bucket=bucket, Key="obj", Tagging={"TagSet": [{"Key": "project", "Value": "apollo"}]})
    assert a.s3.delete_object_tagging(Bucket=bucket, Key="obj")["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert backend().get_object_tagging(Bucket=bucket, Key="obj")["TagSet"] == []
    assert a.s3.get_object_tagging(Bucket=bucket, Key="obj")["TagSet"] == []


def test_tagging_of_missing_key(owned):
    a, bucket = owned
    assert error_of(lambda: a.s3.get_object_tagging(Bucket=bucket, Key="absent")) == (404, "NoSuchKey")


def test_copy_with_replaced_tags(owned):
    a, bucket = owned
    a.s3.put_object_tagging(Bucket=bucket, Key="obj", Tagging={"TagSet": [{"Key": "src", "Value": "1"}]})
    a.s3.copy_object(Bucket=bucket, Key="copy", CopySource={"Bucket": bucket, "Key": "obj"},
                     TaggingDirective="REPLACE", Tagging="dst=2")
    assert tag_map(backend().get_object_tagging(Bucket=bucket, Key="copy")) == {"dst": "2"}


def test_bucket_tagging_not_implemented(owned):
    a, bucket = owned
    assert error_of(lambda: a.s3.put_bucket_tagging(
        Bucket=bucket, Tagging={"TagSet": [{"Key": "k", "Value": "v"}]})) == (501, "NotImplemented")
