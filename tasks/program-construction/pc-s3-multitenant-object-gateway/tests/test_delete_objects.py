"""Batch DeleteObjects, checked against Floci."""

import pytest
from conftest import bucket_contents


@pytest.fixture
def filled(tenants):
    a, b = tenants
    bucket = a.gateway.name("batch")
    a.s3.create_bucket(Bucket=bucket)
    for key in ("one", "two", "dir/three", "keep"):
        a.s3.put_object(Bucket=bucket, Key=key, Body=key.encode())
    return a, b, bucket


def test_batch_delete_removes_listed_keys(filled):
    a, _, bucket = filled
    result = a.s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "one"}, {"Key": "dir/three"}]})
    assert sorted(d["Key"] for d in result["Deleted"]) == ["dir/three", "one"]
    assert result.get("Errors", []) == []
    assert bucket_contents(bucket) == {"two": b"two", "keep": b"keep"}


def test_batch_delete_reports_missing_key_as_deleted(filled):
    a, _, bucket = filled
    result = a.s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "two"}, {"Key": "never-existed"}]})
    assert sorted(d["Key"] for d in result["Deleted"]) == ["never-existed", "two"]
    assert set(bucket_contents(bucket)) == {"one", "dir/three", "keep"}


def test_quiet_batch_delete_lists_nothing(filled):
    a, _, bucket = filled
    result = a.s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "one"}, {"Key": "two"}], "Quiet": True})
    assert result.get("Deleted", []) == []
    assert result.get("Errors", []) == []
    assert set(bucket_contents(bucket)) == {"dir/three", "keep"}


def test_batch_delete_in_foreign_bucket_reports_access_denied(filled):
    a, b, bucket = filled
    result = b.s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "one"}, {"Key": "keep"}]})
    assert result["ResponseMetadata"]["HTTPStatusCode"] == 200
    assert result.get("Deleted", []) == []
    assert sorted((e["Key"], e["Code"]) for e in result["Errors"]) == [("keep", "AccessDenied"), ("one", "AccessDenied")]
    assert set(bucket_contents(bucket)) == {"one", "two", "dir/three", "keep"}
