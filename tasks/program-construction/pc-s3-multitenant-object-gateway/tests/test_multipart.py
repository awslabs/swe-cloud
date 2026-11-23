"""Multipart uploads: create, upload parts, list, complete, abort and part copies, checked against Floci."""

import pytest
from conftest import backend, bucket_contents, error_of

FIRST = bytes(range(256)) * (5 * 1024 * 4)  # 5 MiB, the minimum size of a non-final part
LAST = b"tail-bytes\n" * 100


@pytest.fixture
def owned(tenants):
    a, b = tenants
    bucket = a.gateway.name("mpu")
    a.s3.create_bucket(Bucket=bucket)
    yield a, b, bucket
    # Open uploads would keep the bucket from being removed after the test.
    for key, upload in open_uploads(bucket):
        backend().abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)


def upload_two_parts(s3, bucket, key, **create):
    upload = s3.create_multipart_upload(Bucket=bucket, Key=key, **create)["UploadId"]
    one = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=FIRST)["ETag"]
    two = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=2, Body=LAST)["ETag"]
    return upload, [{"PartNumber": 1, "ETag": one}, {"PartNumber": 2, "ETag": two}]


def open_uploads(bucket):
    return {(u["Key"], u["UploadId"]) for u in backend().list_multipart_uploads(Bucket=bucket).get("Uploads", [])}


def test_completed_upload_stores_concatenated_parts(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "big/object.bin")
    a.s3.complete_multipart_upload(Bucket=bucket, Key="big/object.bin", UploadId=upload, MultipartUpload={"Parts": parts})
    assert bucket_contents(bucket) == {"big/object.bin": FIRST + LAST}


def test_completed_upload_keeps_type_and_metadata_from_create(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "typed", ContentType="application/x-parts", Metadata={"origin": "mpu"})
    a.s3.complete_multipart_upload(Bucket=bucket, Key="typed", UploadId=upload, MultipartUpload={"Parts": parts})
    upstream = backend().head_object(Bucket=bucket, Key="typed")
    assert upstream["ContentType"] == "application/x-parts"
    assert upstream["Metadata"] == {"origin": "mpu"}


def test_complete_returns_upstream_multipart_etag(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "etag")
    done = a.s3.complete_multipart_upload(Bucket=bucket, Key="etag", UploadId=upload, MultipartUpload={"Parts": parts})
    upstream = backend().head_object(Bucket=bucket, Key="etag")
    assert done["ETag"] == upstream["ETag"]
    assert done["ETag"].endswith('-2"')
    assert done["Bucket"] == bucket and done["Key"] == "etag"


def test_list_parts_reports_uploaded_parts(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "parts")
    listed = a.s3.list_parts(Bucket=bucket, Key="parts", UploadId=upload)
    assert [(p["PartNumber"], p["ETag"], p["Size"]) for p in listed["Parts"]] == [
        (1, parts[0]["ETag"], len(FIRST)), (2, parts[1]["ETag"], len(LAST))]
    assert listed["UploadId"] == upload


def test_list_multipart_uploads_shows_open_uploads(owned):
    a, _, bucket = owned
    first = a.s3.create_multipart_upload(Bucket=bucket, Key="one")["UploadId"]
    second = a.s3.create_multipart_upload(Bucket=bucket, Key="two/nested")["UploadId"]
    listed = a.s3.list_multipart_uploads(Bucket=bucket)
    assert {(u["Key"], u["UploadId"]) for u in listed.get("Uploads", [])} == {("one", first), ("two/nested", second)}
    assert open_uploads(bucket) == {("one", first), ("two/nested", second)}


def test_completed_upload_leaves_no_open_upload(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "closed")
    a.s3.complete_multipart_upload(Bucket=bucket, Key="closed", UploadId=upload, MultipartUpload={"Parts": parts})
    assert a.s3.list_multipart_uploads(Bucket=bucket).get("Uploads", []) == []
    assert open_uploads(bucket) == set()


def test_abort_discards_upload(owned):
    a, _, bucket = owned
    upload, _ = upload_two_parts(a.s3, bucket, "dropped")
    assert a.s3.abort_multipart_upload(Bucket=bucket, Key="dropped", UploadId=upload)["ResponseMetadata"]["HTTPStatusCode"] == 204
    assert open_uploads(bucket) == set()
    assert bucket_contents(bucket) == {}
    assert error_of(lambda: a.s3.list_parts(Bucket=bucket, Key="dropped", UploadId=upload)) == (404, "NoSuchUpload")


def test_upload_part_with_unknown_upload_id(owned):
    a, _, bucket = owned
    assert error_of(lambda: a.s3.upload_part(
        Bucket=bucket, Key="k", UploadId="no-such-upload", PartNumber=1, Body=b"x")) == (404, "NoSuchUpload")
    assert bucket_contents(bucket) == {}


def test_complete_with_wrong_part_etag_is_invalid_part(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "bad-etag")
    parts[1]["ETag"] = '"0123456789abcdef0123456789abcdef"'
    assert error_of(lambda: a.s3.complete_multipart_upload(
        Bucket=bucket, Key="bad-etag", UploadId=upload, MultipartUpload={"Parts": parts})) == (400, "InvalidPart")
    assert bucket_contents(bucket) == {}
    assert open_uploads(bucket) == {("bad-etag", upload)}


def test_complete_with_missing_part_is_invalid_part(owned):
    a, _, bucket = owned
    upload, parts = upload_two_parts(a.s3, bucket, "gap")
    claimed = [parts[0], {"PartNumber": 3, "ETag": parts[1]["ETag"]}]
    assert error_of(lambda: a.s3.complete_multipart_upload(
        Bucket=bucket, Key="gap", UploadId=upload, MultipartUpload={"Parts": claimed})) == (400, "InvalidPart")
    assert bucket_contents(bucket) == {}


def test_upload_part_copy_copies_a_byte_range(owned):
    a, _, bucket = owned
    source = bytes(range(256)) * 40
    a.s3.put_object(Bucket=bucket, Key="source", Body=source)
    upload = a.s3.create_multipart_upload(Bucket=bucket, Key="assembled")["UploadId"]
    part = a.s3.upload_part_copy(Bucket=bucket, Key="assembled", UploadId=upload, PartNumber=1,
                                 CopySource={"Bucket": bucket, "Key": "source"}, CopySourceRange="bytes=100-1123")
    etag = part["CopyPartResult"]["ETag"]
    assert [(p["PartNumber"], p["Size"]) for p in a.s3.list_parts(Bucket=bucket, Key="assembled", UploadId=upload)["Parts"]] == [(1, 1024)]
    a.s3.complete_multipart_upload(Bucket=bucket, Key="assembled", UploadId=upload,
                                   MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": etag}]})
    assert bucket_contents(bucket) == {"source": source, "assembled": source[100:1124]}


def test_upload_part_copy_from_foreign_bucket_denied(owned):
    a, b, bucket = owned
    foreign = a.gateway.name("mpu-b")
    b.s3.create_bucket(Bucket=foreign)
    b.s3.put_object(Bucket=foreign, Key="private", Body=b"b's data")
    upload = a.s3.create_multipart_upload(Bucket=bucket, Key="stolen")["UploadId"]
    assert error_of(lambda: a.s3.upload_part_copy(
        Bucket=bucket, Key="stolen", UploadId=upload, PartNumber=1,
        CopySource={"Bucket": foreign, "Key": "private"})) == (403, "AccessDenied")
    assert a.s3.list_parts(Bucket=bucket, Key="stolen", UploadId=upload).get("Parts", []) == []
