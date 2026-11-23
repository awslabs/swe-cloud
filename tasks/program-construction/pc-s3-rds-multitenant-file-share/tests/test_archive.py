"""Zip archive downloads of a whole upload, built from the stored S3 objects."""

import io
import zipfile

from plikenv import basic, error, solo


def entries(resp):
    """Archive entries: name -> (bytes, compression method)."""
    archive = zipfile.ZipFile(io.BytesIO(resp.body))
    return {info.filename: (archive.read(info), info.compress_type) for info in archive.infolist()}


def two_file_upload(alice, **options):
    up = alice.create_upload(**options)
    f = alice.api.add_file(up["id"], "notes.txt", b"archive member one\n").json()
    g = alice.api.add_file(up["id"], "data.bin", bytes(range(64))).json()
    return up, f, g


def test_archive_contains_every_uploaded_file(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice)
    resp = plik.client().get(f"/archive/{up['id']}/bundle.zip")
    assert resp.status == 200, resp
    assert {name: data for name, (data, _) in entries(resp).items()} == {
        "notes.txt": b"archive member one\n",
        "data.bin": bytes(range(64)),
    }


def test_archive_response_headers(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice)
    resp = plik.client().get(f"/archive/{up['id']}/bundle.zip")
    assert resp.headers["Content-Type"] == "application/zip"
    assert resp.headers["Content-Disposition"] == 'filename="bundle.zip"'


def test_archive_download_parameter_makes_an_attachment(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice)
    resp = plik.client().get(f"/archive/{up['id']}/bundle.zip?dl=1")
    assert resp.status == 200, resp
    assert resp.headers["Content-Disposition"] == 'attachment; filename="bundle.zip"'


def test_archive_name_must_end_in_zip(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice)
    error(plik.client().get(f"/archive/{up['id']}/bundle.tar"), 400, "invalid archive name, missing .zip extension")


def test_archive_of_upload_without_files_is_refused(plik):
    alice = solo(plik)
    up = alice.create_upload()
    error(plik.client().get(f"/archive/{up['id']}/empty.zip"), 400, "nothing to archive")


def test_archive_skips_removed_files(plik):
    alice = solo(plik)
    up, f, _ = two_file_upload(alice)
    assert alice.api.delete(f"/file/{up['id']}/{f['id']}/notes.txt").status == 200
    resp = plik.client().get(f"/archive/{up['id']}/bundle.zip")
    assert sorted(entries(resp)) == ["data.bin"]


def test_archive_entries_are_compressed_by_default(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice)
    resp = plik.client().get(f"/archive/{up['id']}/bundle.zip")
    assert {method for _, method in entries(resp).values()} == {zipfile.ZIP_DEFLATED}


def test_archive_compression_can_be_disabled(plik):
    alice = solo(plik, extra="EnableArchiveCompression = false")
    up, _, _ = two_file_upload(alice)
    resp = plik.client().get(f"/archive/{up['id']}/bundle.zip")
    got = entries(resp)
    assert {method for _, method in got.values()} == {zipfile.ZIP_STORED}
    assert got["data.bin"][0] == bytes(range(64))


def test_one_shot_archive_is_served_once(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice, oneShot=True)
    first = plik.client().get(f"/archive/{up['id']}/once.zip")
    assert first.status == 200 and sorted(entries(first)) == ["data.bin", "notes.txt"]
    error(plik.client().get(f"/archive/{up['id']}/once.zip"), 400, "nothing to archive")
    assert sorted(plik.sql("SELECT status FROM files")) == [("removed",), ("removed",)]


def test_password_protected_archive_requires_credentials(plik):
    alice = solo(plik)
    up, _, _ = two_file_upload(alice, protectedByPassword=True, login="share", password="zip-pass")
    resp = plik.client().get(f"/archive/{up['id']}/locked.zip")
    error(resp, 401, "please provide valid credentials to access this upload : missing Authorization header")
    resp = plik.client().get(f"/archive/{up['id']}/locked.zip", headers=basic("share", "zip-pass"))
    assert resp.status == 200 and sorted(entries(resp)) == ["data.bin", "notes.txt"]


def test_archive_of_a_stream_upload_is_refused(plik):
    alice = solo(plik)
    up = alice.create_upload(stream=True)
    error(plik.client().get(f"/archive/{up['id']}/s.zip"), 400, "archive feature is not available in stream mode")
