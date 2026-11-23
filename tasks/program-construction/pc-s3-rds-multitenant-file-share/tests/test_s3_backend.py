"""S3 data backend: server-side encryption, part size, key layout, bucket creation, legacy keys and versioned buckets."""

import json

import pytest
from botocore.exceptions import ClientError

from plikenv import solo

PART = 5 * 1024 * 1024


def keys(plik):
    """Object keys in the bucket, without reading bodies (SSE-C bodies need a key)."""
    out = []
    for page in plik.s3.get_paginator("list_objects_v2").paginate(Bucket=plik.bucket):
        out += [item["Key"] for item in page.get("Contents", [])]
    return sorted(out)


def head(plik, key, **extra):
    return plik.s3.head_object(Bucket=plik.bucket, Key=key, **extra)


def sse_c_key(plik, file_id):
    (details,) = plik.sql("SELECT backend_details FROM files WHERE id = %s", file_id)[0]
    return json.loads(details)["SSEKey"]


def test_sse_s3_encrypts_stored_objects(plik):
    alice = solo(plik, data_extra='    SSE = "S3"')
    up, f = alice.upload("enc.txt", b"encrypted at rest")
    assert head(plik, plik.key(up["id"], f["id"])).get("ServerSideEncryption") == "AES256"


def test_sse_c_object_cannot_be_read_without_a_key(plik):
    alice = solo(plik, data_extra='    SSE = "SSE-C"')
    up, f = alice.upload("secret.txt", b"customer keyed")
    assert keys(plik) == [plik.key(up["id"], f["id"])]
    with pytest.raises(ClientError):
        plik.s3.get_object(Bucket=plik.bucket, Key=plik.key(up["id"], f["id"]))


def test_sse_c_key_recorded_for_the_file_decrypts_the_object(plik):
    alice = solo(plik, data_extra='    SSE = "SSE-C"')
    up, f = alice.upload("secret.txt", b"customer keyed")
    key = sse_c_key(plik, f["id"])
    body = plik.s3.get_object(
        Bucket=plik.bucket, Key=plik.key(up["id"], f["id"]), SSECustomerAlgorithm="AES256", SSECustomerKey=key.encode()
    )["Body"].read()
    assert body == b"customer keyed"


def test_sse_c_keys_differ_per_file(plik):
    alice = solo(plik, data_extra='    SSE = "SSE-C"')
    up = alice.create_upload()
    first = alice.api.add_file(up["id"], "a.txt", b"first").json()
    second = alice.api.add_file(up["id"], "b.txt", b"second").json()
    assert sse_c_key(plik, first["id"]) != sse_c_key(plik, second["id"])


def test_sse_c_files_download_through_the_server(plik):
    alice = solo(plik, data_extra='    SSE = "SSE-C"')
    up, f = alice.upload("secret.txt", b"customer keyed")
    resp = plik.client().get(f"/file/{up['id']}/{f['id']}/secret.txt")
    assert resp.status == 200 and resp.body == b"customer keyed", resp


def test_sse_c_file_removal_deletes_the_object(plik):
    alice = solo(plik, data_extra='    SSE = "SSE-C"')
    up, f = alice.upload("secret.txt", b"customer keyed")
    resp = alice.api.delete(f"/file/{up['id']}/{f['id']}/secret.txt")
    assert resp.status == 200, resp
    assert keys(plik) == []


def test_file_larger_than_part_size_is_stored_in_parts(plik):
    alice = solo(plik, data_extra=f"    PartSize = {PART}")
    up = alice.create_upload()
    data = bytes(range(256)) * (PART // 256 + 4096)
    f = alice.api.add_file(up["id"], "big.bin", data).json()
    info = head(plik, plik.key(up["id"], f["id"]))
    assert info["ContentLength"] == len(data)
    assert info["ETag"].strip('"').endswith("-2"), info["ETag"]


def test_file_of_exactly_part_size_is_stored_in_one_request(plik):
    alice = solo(plik, data_extra=f"    PartSize = {PART}")
    up = alice.create_upload()
    data = b"p" * PART
    f = alice.api.add_file(up["id"], "edge.bin", data).json()
    info = head(plik, plik.key(up["id"], f["id"]))
    assert info["ContentLength"] == PART
    assert "-" not in info["ETag"].strip('"'), info["ETag"]


def test_multipart_file_downloads_byte_exact(plik):
    alice = solo(plik, data_extra=f"    PartSize = {PART}")
    up = alice.create_upload()
    data = bytes(range(251)) * (PART // 251 + 3000)
    f = alice.api.add_file(up["id"], "big.bin", data).json()
    resp = plik.client().get(f"/file/{up['id']}/{f['id']}/big.bin")
    assert resp.status == 200 and resp.body == data


def test_part_size_below_minimum_is_rejected_at_startup(plik):
    plik.write_config(data_extra="    PartSize = 1048576")
    plik.add_user("alice", "alice-secret-1", "-1")
    with pytest.raises(AssertionError, match="server exited"):
        plik.start()


def test_empty_prefix_stores_objects_at_bucket_root(plik):
    alice = solo(plik, prefix="")
    up, f = alice.upload("root.txt", b"no prefix")
    assert keys(plik) == [f"{up['id']}.{f['id']}"]


def test_missing_bucket_is_created_at_startup(plik):
    plik.s3.delete_bucket(Bucket=plik.bucket)
    alice = solo(plik)
    assert plik.bucket in [b["Name"] for b in plik.s3.list_buckets()["Buckets"]]
    up, f = alice.upload("first.txt", b"into a new bucket")
    assert keys(plik) == [plik.key(up["id"], f["id"])]


def test_legacy_file_id_key_is_served_when_upload_key_is_absent(plik):
    alice = solo(plik)
    up, f = alice.upload("old.txt", b"current")
    plik.s3.delete_object(Bucket=plik.bucket, Key=plik.key(up["id"], f["id"]))
    plik.s3.put_object(Bucket=plik.bucket, Key=f"tenants/{f['id']}", Body=b"legacy layout")
    resp = plik.client().get(f"/file/{up['id']}/{f['id']}/old.txt")
    assert resp.status == 200 and resp.body == b"legacy layout", resp


def test_legacy_file_id_key_is_removed_with_the_file(plik):
    alice = solo(plik)
    up, f = alice.upload("old.txt", b"current")
    plik.s3.delete_object(Bucket=plik.bucket, Key=plik.key(up["id"], f["id"]))
    plik.s3.put_object(Bucket=plik.bucket, Key=f"tenants/{f['id']}", Body=b"legacy layout")
    plik.s3.put_object(Bucket=plik.bucket, Key="tenants/unrelated", Body=b"keep")
    resp = alice.api.delete(f"/file/{up['id']}/{f['id']}/old.txt")
    assert resp.status == 200, resp
    assert keys(plik) == ["tenants/unrelated"]


def test_versioned_bucket_file_removal_leaves_no_versions(plik):
    plik.s3.put_bucket_versioning(Bucket=plik.bucket, VersioningConfiguration={"Status": "Enabled"})
    try:
        alice = solo(plik)
        up, f = alice.upload("v.txt", b"versioned bytes")
        keep, g = alice.upload("w.txt", b"kept")
        resp = alice.api.delete(f"/file/{up['id']}/{f['id']}/v.txt")
        assert resp.status == 200, resp
        listing = plik.s3.list_object_versions(Bucket=plik.bucket)
        assert [v["Key"] for v in listing.get("Versions", [])] == [plik.key(keep["id"], g["id"])]
        assert listing.get("DeleteMarkers", []) == []
    finally:
        listing = plik.s3.list_object_versions(Bucket=plik.bucket)
        for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            plik.s3.delete_object(Bucket=plik.bucket, Key=item["Key"], VersionId=item["VersionId"])


def test_stored_object_content_type(plik):
    alice = solo(plik)
    up, f = alice.upload("typed.txt", b"plain text content\n")
    assert f["fileType"] == "text/plain; charset=utf-8"
    assert head(plik, plik.key(up["id"], f["id"]))["ContentType"] == "application/octet-stream"
