"""Object-ownership controls and bucket location, forwarded to the upstream bucket."""

import pytest
from conftest import backend, error_of


@pytest.fixture
def owned(tenants):
    a, _ = tenants
    bucket = a.gateway.name("own-ctl")
    a.s3.create_bucket(Bucket=bucket)
    return a, bucket


def rule(response):
    return response["OwnershipControls"]["Rules"][0]["ObjectOwnership"]


def test_new_bucket_has_no_ownership_controls(owned):
    a, bucket = owned
    assert error_of(lambda: a.s3.get_bucket_ownership_controls(Bucket=bucket)) == (404, "OwnershipControlsNotFoundError")


def test_put_ownership_controls_stored_upstream(owned):
    a, bucket = owned
    a.s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]})
    assert rule(backend().get_bucket_ownership_controls(Bucket=bucket)) == "BucketOwnerPreferred"
    assert rule(a.s3.get_bucket_ownership_controls(Bucket=bucket)) == "BucketOwnerPreferred"


def test_delete_ownership_controls(owned):
    a, bucket = owned
    a.s3.put_bucket_ownership_controls(Bucket=bucket, OwnershipControls={"Rules": [{"ObjectOwnership": "ObjectWriter"}]})
    assert a.s3.delete_bucket_ownership_controls(Bucket=bucket)["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert error_of(lambda: a.s3.get_bucket_ownership_controls(Bucket=bucket)) == (404, "OwnershipControlsNotFoundError")


def test_get_bucket_location(owned):
    a, bucket = owned
    assert a.s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") is None
