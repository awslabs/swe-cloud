"""Bucket versioning: configuration, version IDs, delete markers and version listings, checked against Floci."""

import pytest
from conftest import backend, error_of


@pytest.fixture
def owned(tenants):
    a, _ = tenants
    bucket = a.gateway.name("vers")
    a.s3.create_bucket(Bucket=bucket)
    yield a, bucket
    # Old versions and delete markers would keep the bucket from being removed after the test.
    s3 = backend()
    listed = s3.list_object_versions(Bucket=bucket)
    for entry in listed.get("Versions", []) + listed.get("DeleteMarkers", []):
        s3.delete_object(Bucket=bucket, Key=entry["Key"], VersionId=entry["VersionId"])


@pytest.fixture
def versioned(owned):
    a, bucket = owned
    a.s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    first = a.s3.put_object(Bucket=bucket, Key="doc", Body=b"first")["VersionId"]
    second = a.s3.put_object(Bucket=bucket, Key="doc", Body=b"second")["VersionId"]
    return a, bucket, first, second


def upstream_versions(bucket):
    listed = backend().list_object_versions(Bucket=bucket)
    return ({(v["Key"], v["VersionId"]) for v in listed.get("Versions", [])},
            {(m["Key"], m["VersionId"]) for m in listed.get("DeleteMarkers", [])})


def test_new_bucket_has_no_versioning_status(owned):
    a, bucket = owned
    assert "Status" not in a.s3.get_bucket_versioning(Bucket=bucket)


def test_enabling_versioning_is_stored_upstream(owned):
    a, bucket = owned
    a.s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    assert backend().get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"
    assert a.s3.get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"


def test_suspending_versioning(versioned):
    a, bucket, *_ = versioned
    a.s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Suspended"})
    assert a.s3.get_bucket_versioning(Bucket=bucket)["Status"] == "Suspended"
    assert backend().get_bucket_versioning(Bucket=bucket)["Status"] == "Suspended"


def test_invalid_versioning_status_is_malformed(owned):
    a, bucket = owned
    assert error_of(lambda: a.s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Sometimes"})) == (400, "MalformedXML")
    assert "Status" not in backend().get_bucket_versioning(Bucket=bucket)


def test_put_returns_upstream_version_ids(versioned):
    _, bucket, first, second = versioned
    assert first and second and first != second
    assert upstream_versions(bucket) == ({("doc", first), ("doc", second)}, set())


def test_get_and_head_older_version(versioned):
    a, bucket, first, _ = versioned
    got = a.s3.get_object(Bucket=bucket, Key="doc", VersionId=first)
    assert got["Body"].read() == b"first"
    assert got["VersionId"] == first
    assert a.s3.head_object(Bucket=bucket, Key="doc", VersionId=first)["ContentLength"] == 5
    assert a.s3.get_object(Bucket=bucket, Key="doc")["Body"].read() == b"second"


def test_delete_without_version_adds_delete_marker(versioned):
    a, bucket, first, second = versioned
    deleted = a.s3.delete_object(Bucket=bucket, Key="doc")
    assert deleted["DeleteMarker"] is True
    marker = deleted["VersionId"]
    assert upstream_versions(bucket) == ({("doc", first), ("doc", second)}, {("doc", marker)})
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="doc")) == (404, "NoSuchKey")
    assert a.s3.get_object(Bucket=bucket, Key="doc", VersionId=second)["Body"].read() == b"second"


def test_list_object_versions(versioned):
    a, bucket, first, second = versioned
    marker = a.s3.delete_object(Bucket=bucket, Key="doc")["VersionId"]
    a.s3.put_object(Bucket=bucket, Key="other", Body=b"o")
    listed = a.s3.list_object_versions(Bucket=bucket, Prefix="doc")
    assert {(v["Key"], v["VersionId"], v["Size"]) for v in listed["Versions"]} == {("doc", first, 5), ("doc", second, 6)}
    assert [(m["Key"], m["VersionId"], m["IsLatest"]) for m in listed["DeleteMarkers"]] == [("doc", marker, True)]


def test_delete_specific_version_removes_it(versioned):
    a, bucket, first, second = versioned
    deleted = a.s3.delete_object(Bucket=bucket, Key="doc", VersionId=first)
    assert deleted["VersionId"] == first
    assert upstream_versions(bucket) == ({("doc", second)}, set())
