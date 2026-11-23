"""Object listing parameters beyond prefix, delimiter and plain paging."""

import pytest

KEYS = ["a/1", "a/2", "b/1", "c", "d/1", "d/2", "e"]


@pytest.fixture
def listed(tenants):
    a, _ = tenants
    bucket = a.gateway.name("lst")
    a.s3.create_bucket(Bucket=bucket)
    for key in KEYS:
        a.s3.put_object(Bucket=bucket, Key=key, Body=b"1")
    return a, bucket


def test_list_v2_start_after(listed):
    a, bucket = listed
    page = a.s3.list_objects_v2(Bucket=bucket, StartAfter="c")
    assert [o["Key"] for o in page["Contents"]] == ["d/1", "d/2", "e"]


def test_list_v2_delimiter_pages_roll_up_prefixes(listed):
    a, bucket = listed
    keys, prefixes, token = [], [], None
    for _ in range(10):
        kwargs = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": 2}
        if token:
            kwargs["ContinuationToken"] = token
        page = a.s3.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in page.get("Contents", [])]
        prefixes += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        if not page["IsTruncated"]:
            break
        token = page["NextContinuationToken"]
    assert keys == ["c", "e"]
    assert prefixes == ["a/", "b/", "d/"]


def test_list_v1_delimiter_with_marker_paging(listed):
    a, bucket = listed
    first = a.s3.list_objects(Bucket=bucket, Delimiter="/", MaxKeys=3)
    assert first["IsTruncated"] is True
    assert [p["Prefix"] for p in first["CommonPrefixes"]] == ["a/", "b/"]
    assert [o["Key"] for o in first["Contents"]] == ["c"]
    rest = a.s3.list_objects(Bucket=bucket, Delimiter="/", Marker=first["NextMarker"])
    assert [p["Prefix"] for p in rest.get("CommonPrefixes", [])] == ["d/"]
    assert [o["Key"] for o in rest["Contents"]] == ["e"]


def test_list_v2_of_unmatched_prefix_is_empty(listed):
    a, bucket = listed
    page = a.s3.list_objects_v2(Bucket=bucket, Prefix="zzz")
    assert page["KeyCount"] == 0
    assert "Contents" not in page
