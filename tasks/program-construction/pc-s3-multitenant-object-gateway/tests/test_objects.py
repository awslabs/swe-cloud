"""Owner object operations: PUT/GET/HEAD/DELETE and listings, checked against Floci."""

import pytest
from conftest import backend, bucket_contents, error_of

BINARY = bytes(range(256)) * 3 + b"\x00\xff\r\n"


@pytest.fixture
def owned(tenants):
    a, b = tenants
    bucket = a.gateway.name("objs")
    a.s3.create_bucket(Bucket=bucket)
    return a, b, bucket


def test_put_stores_bytes_and_metadata_upstream(owned):
    a, _, bucket = owned
    put = a.s3.put_object(Bucket=bucket, Key="docs/note.txt", Body=b"hello tenant\n",
                          ContentType="text/x-note", Metadata={"color": "red", "shape": "round"})
    a.s3.put_object(Bucket=bucket, Key="blob.bin", Body=BINARY)
    a.s3.put_object(Bucket=bucket, Key="empty", Body=b"")
    assert bucket_contents(bucket) == {"docs/note.txt": b"hello tenant\n", "blob.bin": BINARY, "empty": b""}
    upstream = backend().head_object(Bucket=bucket, Key="docs/note.txt")
    assert upstream["ContentType"] == "text/x-note"
    assert upstream["Metadata"] == {"color": "red", "shape": "round"}
    assert put["ETag"] == upstream["ETag"]


def test_get_and_head_return_stored_object(owned):
    a, _, bucket = owned
    backend_s3 = backend()
    a.s3.put_object(Bucket=bucket, Key="docs/note.txt", Body=b"hello tenant\n",
                    ContentType="text/x-note", Metadata={"color": "red"})
    a.s3.put_object(Bucket=bucket, Key="blob.bin", Body=BINARY)
    upstream = backend_s3.head_object(Bucket=bucket, Key="docs/note.txt")

    got = a.s3.get_object(Bucket=bucket, Key="docs/note.txt")
    assert got["Body"].read() == b"hello tenant\n"
    assert got["ContentType"] == "text/x-note"
    assert got["Metadata"] == {"color": "red"}
    assert got["ContentLength"] == 13
    assert got["ETag"] == upstream["ETag"]

    head = a.s3.head_object(Bucket=bucket, Key="docs/note.txt")
    assert head["ContentLength"] == 13
    assert head["ContentType"] == "text/x-note"
    assert head["Metadata"] == {"color": "red"}
    assert head["ETag"] == upstream["ETag"]

    assert a.s3.get_object(Bucket=bucket, Key="blob.bin")["Body"].read() == BINARY
    assert a.s3.head_object(Bucket=bucket, Key="blob.bin")["ContentLength"] == len(BINARY)


def test_overwrite_replaces_bytes(owned):
    a, _, bucket = owned
    a.s3.put_object(Bucket=bucket, Key="same", Body=b"first version")
    a.s3.put_object(Bucket=bucket, Key="same", Body=b"2nd")
    assert bucket_contents(bucket) == {"same": b"2nd"}
    assert a.s3.get_object(Bucket=bucket, Key="same")["Body"].read() == b"2nd"


def test_missing_key(owned):
    a, _, bucket = owned
    a.s3.put_object(Bucket=bucket, Key="present", Body=b"p")
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="absent")) == (404, "NoSuchKey")
    assert error_of(lambda: a.s3.head_object(Bucket=bucket, Key="absent"))[0] == 404
    assert bucket_contents(bucket) == {"present": b"p"}


def test_delete_object(owned):
    a, _, bucket = owned
    a.s3.put_object(Bucket=bucket, Key="gone", Body=b"g")
    a.s3.put_object(Bucket=bucket, Key="stays", Body=b"s")
    assert a.s3.delete_object(Bucket=bucket, Key="gone")["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert bucket_contents(bucket) == {"stays": b"s"}
    assert error_of(lambda: a.s3.get_object(Bucket=bucket, Key="gone")) == (404, "NoSuchKey")
    # Deleting an absent key by the owner succeeds without side effects.
    assert a.s3.delete_object(Bucket=bucket, Key="never")["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert bucket_contents(bucket) == {"stays": b"s"}


def test_list_objects_v2_prefix_and_delimiter(owned):
    a, _, bucket = owned
    for key, body in (("dir/a.txt", b"a"), ("dir/sub/b.txt", b""), ("dir/sub/c.txt", b"cc"), ("top.txt", b"x" * 3000)):
        a.s3.put_object(Bucket=bucket, Key=key, Body=body)

    full = a.s3.list_objects_v2(Bucket=bucket)
    assert [(o["Key"], o["Size"]) for o in full["Contents"]] == [
        ("dir/a.txt", 1), ("dir/sub/b.txt", 0), ("dir/sub/c.txt", 2), ("top.txt", 3000)]
    assert full["KeyCount"] == 4 and full["IsTruncated"] is False

    nested = a.s3.list_objects_v2(Bucket=bucket, Prefix="dir/", Delimiter="/")
    assert [o["Key"] for o in nested["Contents"]] == ["dir/a.txt"]
    assert [p["Prefix"] for p in nested.get("CommonPrefixes", [])] == ["dir/sub/"]
    assert nested["KeyCount"] == 2


def test_list_objects_v2_pages(owned):
    a, _, bucket = owned
    keys = ["k1", "k2", "k3", "k4", "k5"]
    for key in keys:
        a.s3.put_object(Bucket=bucket, Key=key, Body=key.encode())
    seen, token, pages = [], None, 0
    while True:
        kwargs = {"Bucket": bucket, "MaxKeys": 2}
        if token:
            kwargs["ContinuationToken"] = token
        page = a.s3.list_objects_v2(**kwargs)
        pages += 1
        seen += [o["Key"] for o in page.get("Contents", [])]
        assert len(page.get("Contents", [])) <= 2
        if not page["IsTruncated"]:
            break
        token = page["NextContinuationToken"]
        assert pages < 10
    assert seen == keys
    assert pages == 3


def test_list_objects_v1(owned):
    a, _, bucket = owned
    for key in ("alpha", "beta", "gamma"):
        a.s3.put_object(Bucket=bucket, Key=key, Body=b"v1")
    listing = a.s3.list_objects(Bucket=bucket)
    assert [o["Key"] for o in listing["Contents"]] == ["alpha", "beta", "gamma"]
    after = a.s3.list_objects(Bucket=bucket, Marker="alpha")
    assert [o["Key"] for o in after["Contents"]] == ["beta", "gamma"]


def test_keys_needing_uri_encoding(owned):
    a, _, bucket = owned
    keys = ["with space.txt", "plus+sign&amp=eq.txt", "unicodé/ñ.bin", "tilde~star*(x).txt", "%41percent", "a/b/../c"]
    for key in keys:
        a.s3.put_object(Bucket=bucket, Key=key, Body=key.encode())
    assert bucket_contents(bucket) == {key: key.encode() for key in keys}
    for key in keys:
        assert a.s3.get_object(Bucket=bucket, Key=key)["Body"].read() == key.encode(), key
    assert sorted(o["Key"] for o in a.s3.list_objects_v2(Bucket=bucket)["Contents"]) == sorted(keys)
    a.s3.delete_object(Bucket=bucket, Key="with space.txt")
    assert set(bucket_contents(bucket)) == set(keys) - {"with space.txt"}
