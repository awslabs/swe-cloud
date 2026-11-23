"""Media uploads to S3: object keys and types, URLs, thumbnails, renaming, listing and deletion."""

import re
import urllib.parse

import boto3
import pytest

from mailer import ENDPOINT, assert_id, assert_json, multipart, png_bytes, png_size

MEDIA_FIELDS = ("filename", "content_type", "provider", "meta", "url", "thumb_url")


def use_s3(bucket, path="/media/x/", bucket_type="public", public_url=""):
    def change(settings):
        settings.update({
            "upload.provider": "s3", "upload.s3.url": ENDPOINT + "/", "upload.s3.bucket": bucket,
            "upload.s3.aws_access_key_id": "test", "upload.s3.aws_secret_access_key": "test",
            "upload.s3.aws_default_region": "us-east-1", "upload.s3.bucket_path": path,
            "upload.s3.bucket_type": bucket_type, "upload.s3.public_url": public_url,
        })
    return change


def objects(bucket):
    s3 = boto3.client("s3")
    out = {}
    for item in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
        got = s3.get_object(Bucket=bucket, Key=item["Key"])
        out[item["Key"]] = (got["ContentType"], got["Body"].read())
    return out


def media_rows(app):
    return app.q("SELECT filename, thumb, content_type, provider, meta FROM media ORDER BY id")


@pytest.fixture
def s3app(app, bucket):
    app.configure(use_s3(bucket))
    return app, bucket


def upload_ok(app, filename, ctype, data):
    status, payload = app.upload(filename, ctype, data)
    assert status == 200, (filename, status, payload)
    return payload["data"]


def test_text_upload(s3app):
    app, bucket = s3app
    assert app.q("SELECT value FROM settings WHERE key IN ('upload.provider', 'upload.s3.bucket', 'upload.s3.bucket_path') ORDER BY key") == [
        ("s3",), (bucket,), ("/media/x/",)]
    assert app.get("/api/media")["results"] == []
    media = upload_ok(app, "notes file.txt", "text/plain", b"hello\n")
    base = f"{ENDPOINT}/{bucket}/media/x/"
    assert_json({k: media[k] for k in MEDIA_FIELDS},
                {"filename": "notes-file.txt", "content_type": "text/plain", "provider": "s3", "meta": {}, "url": base + "notes-file.txt", "thumb_url": None})
    assert_id(media["id"])
    assert len(media["uuid"]) == 36
    assert objects(bucket) == {"media/x/notes-file.txt": ("text/plain", b"hello\n")}
    assert media_rows(app) == [("notes-file.txt", "", "text/plain", "s3", {})]
    assert app.q("SELECT uuid::text FROM media") == [(media["uuid"],)]

    other = upload_ok(app, "data.bin", "application/x-custom", b"\x00\x01\xff")
    assert other["url"] == base + "data.bin"
    assert objects(bucket)["media/x/data.bin"] == ("application/x-custom", b"\x00\x01\xff")


def test_duplicate_filename_is_renamed(s3app):
    app, bucket = s3app
    upload_ok(app, "report.txt", "text/plain", b"first")
    second = upload_ok(app, "report.txt", "text/plain", b"second")
    match = re.fullmatch(r"report_([0-9A-Za-z]{6})\.txt", second["filename"])
    assert match, second["filename"]
    assert second["url"] == f"{ENDPOINT}/{bucket}/media/x/{second['filename']}"
    assert objects(bucket) == {"media/x/report.txt": ("text/plain", b"first"), f"media/x/{second['filename']}": ("text/plain", b"second")}
    assert [r[0] for r in media_rows(app)] == ["report.txt", second["filename"]]


def test_image_thumbnails(s3app):
    app, bucket = s3app
    base = f"{ENDPOINT}/{bucket}/media/x/"
    original = png_bytes(40, 30)
    media = upload_ok(app, "Red Dot.PNG", "image/png", original)
    assert_json({k: media[k] for k in MEDIA_FIELDS},
                {"filename": "Red-Dot.PNG", "content_type": "image/png", "provider": "s3", "meta": {"width": 40, "height": 30},
                 "url": base + "Red-Dot.PNG", "thumb_url": base + "thumb_Red-Dot.PNG"})
    stored = objects(bucket)
    assert set(stored) == {"media/x/Red-Dot.PNG", "media/x/thumb_Red-Dot.PNG"}
    assert stored["media/x/Red-Dot.PNG"] == ("image/png", original)
    ctype, thumb = stored["media/x/thumb_Red-Dot.PNG"]
    assert ctype == "image/png" and png_size(thumb) == (250, 188)

    # The thumbnail is a PNG but carries the upload's content type.
    wide = upload_ok(app, "wide.jpg", "image/jpeg", png_bytes(600, 100, (0, 0, 255)))
    assert wide["meta"] == {"width": 600, "height": 100} and wide["thumb_url"] == base + "thumb_wide.jpg"
    ctype, thumb = objects(bucket)["media/x/thumb_wide.jpg"]
    assert ctype == "image/jpeg" and png_size(thumb) == (250, 42)
    assert media_rows(app) == [
        ("Red-Dot.PNG", "thumb_Red-Dot.PNG", "image/png", "s3", {"width": 40, "height": 30}),
        ("wide.jpg", "thumb_wide.jpg", "image/jpeg", "s3", {"width": 600, "height": 100}),
    ]

    vector = upload_ok(app, "vec.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    assert (vector["url"], vector["thumb_url"], vector["meta"]) == (base + "vec.svg", base + "vec.svg", {})
    assert media_rows(app)[-1] == ("vec.svg", "vec.svg", "image/svg+xml", "s3", {})

    before = (objects(bucket), media_rows(app))
    status, payload = app.upload("broken.gif", "image/gif", b"GIF89a garbage")
    assert status == 500 and set(payload) == {"message"}
    assert (objects(bucket), media_rows(app)) == before


def test_listing_fetching_and_deleting(s3app):
    app, bucket = s3app
    note = upload_ok(app, "notes.txt", "text/plain", b"n")
    image = upload_ok(app, "pic.png", "image/png", png_bytes(10, 10))
    other = upload_ok(app, "other.txt", "text/plain", b"o")
    listing = app.get("/api/media")
    assert sorted(item["filename"] for item in listing["results"]) == ["notes.txt", "other.txt", "pic.png"]
    assert_json(listing["total"], 3)
    by_name = {item["filename"]: item for item in listing["results"]}
    assert by_name["pic.png"]["thumb_url"] == image["thumb_url"] and by_name["notes.txt"]["url"] == note["url"]
    assert [item["filename"] for item in app.get("/api/media?query=note")["results"]] == ["notes.txt"]
    fetched = app.get(f"/api/media/{image['id']}")
    assert_json({k: fetched[k] for k in ("id", "uuid", *MEDIA_FIELDS)}, {k: image[k] for k in ("id", "uuid", *MEDIA_FIELDS)})
    assert app.call("GET", "/api/media/99999")[0] == 404

    assert app.ok("DELETE", f"/api/media/{image['id']}") is True
    assert set(objects(bucket)) == {"media/x/notes.txt", "media/x/other.txt"}
    assert [r[0] for r in media_rows(app)] == ["notes.txt", "other.txt"]
    assert app.call("GET", f"/api/media/{image['id']}")[0] == 404
    status, _ = app.call("DELETE", "/api/media/99999")
    assert status == 500
    assert set(objects(bucket)) == {"media/x/notes.txt", "media/x/other.txt"} and len(media_rows(app)) == 2
    assert app.ok("DELETE", f"/api/media/{other['id']}") is True
    assert set(objects(bucket)) == {"media/x/notes.txt"}

    body, ctype = multipart({"x": "y"})
    assert app.call("POST", "/api/media", body, ctype=ctype)[0] == 400


def test_extension_allowlist(s3app):
    app, bucket = s3app
    app.configure(lambda s: s.update({"upload.extensions": [" .PNG", "txt "], "upload.s3.aws_secret_access_key": ""}))
    assert app.q("SELECT value FROM settings WHERE key = 'upload.extensions'") == [(["png", "txt"],)]
    assert app.settings()["upload.extensions"] == ["png", "txt"]
    status, payload = app.upload("doc.pdf", "application/pdf", b"%PDF-1.4")
    assert status == 400 and set(payload) == {"message"}
    assert objects(bucket) == {} and media_rows(app) == []
    upload_ok(app, "ok.TXT", "text/plain", b"ok")
    assert objects(bucket) == {"media/x/ok.TXT": ("text/plain", b"ok")}


def test_bucket_root_and_public_url(app, bucket):
    app.configure(use_s3(bucket, path="/", public_url="https://cdn.example.test/m"))
    media = upload_ok(app, "root.txt", "text/plain", b"r")
    assert media["url"] == "https://cdn.example.test/m/root.txt"
    assert objects(bucket) == {"root.txt": ("text/plain", b"r")}
    image = upload_ok(app, "logo.png", "image/png", png_bytes(5, 5))
    assert image["thumb_url"] == "https://cdn.example.test/m/thumb_logo.png"
    assert set(objects(bucket)) == {"root.txt", "logo.png", "thumb_logo.png"}
    # Changing the path afterwards affects the URLs reported for existing media.
    app.configure(use_s3(bucket, path="nested/dir", public_url=""))
    assert app.get(f"/api/media/{media['id']}")["url"] == f"{ENDPOINT}/{bucket}/nested/dir/root.txt"
    later = upload_ok(app, "later.txt", "text/plain", b"l")
    assert later["url"] == f"{ENDPOINT}/{bucket}/nested/dir/later.txt"
    assert "nested/dir/later.txt" in objects(bucket)


def test_private_bucket_presigned_urls(app, bucket):
    app.configure(use_s3(bucket, path="priv", bucket_type="private"))
    media = upload_ok(app, "p.txt", "text/plain", b"p")
    url = urllib.parse.urlsplit(media["url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ENDPOINT}/{bucket}/priv/p.txt"
    query = urllib.parse.parse_qs(url.query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["601200"]
    assert query["X-Amz-Credential"][0].startswith("test/") and query["X-Amz-Credential"][0].endswith("/us-east-1/s3/aws4_request")
    assert re.fullmatch(r"[0-9a-f]{64}", query["X-Amz-Signature"][0])
    assert objects(bucket) == {"priv/p.txt": ("text/plain", b"p")}
