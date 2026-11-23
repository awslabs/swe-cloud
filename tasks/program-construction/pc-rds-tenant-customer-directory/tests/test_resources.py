"""Resources: files uploaded through an S3 storage provider, their stored records, and deletion."""

import boto3
import pytest

from support import (
    envelope,
    error,
    is_count,
    ok,
    parse_time,
    refused,
    storage_provider,
    upload,
    wider_init,
)

CDN = "https://cdn.example.test/"


@pytest.fixture
def store(launch, bucket):
    """acme's application uses global provider 'files'; 'files-cdn' adds a path prefix and a public domain."""
    providers = [
        storage_provider("files", bucket),
        storage_provider("files-cdn", bucket, pathPrefix="tenant-files", domain=CDN),
        storage_provider("files-acme", bucket, owner="acme"),
    ]
    d = launch(wider_init(providers=providers, app_providers=["files"]))
    d.bucket = bucket
    return d


def objects(bucket):
    s3 = boto3.client("s3")
    return {item["Key"]: s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            for item in s3.list_objects_v2(Bucket=bucket).get("Contents", [])}


def resources(d, owner="acme", cred="acme"):
    listing = ok(d.get(f"/api/get-resources?owner={owner}&user=dana", cred))
    assert isinstance(listing, list), listing
    return {r["name"]: r for r in listing}


def stored_resources(d):
    return d.sql("SELECT * FROM resource ORDER BY owner, name")


def test_upload_puts_exact_bytes_in_bucket(store):
    payload = bytes(range(256)) + b"\x00tail"
    ok(upload(store, "resource/acme/dana/blob.bin", payload, "application/octet-stream"))
    assert objects(store.bucket) == {"resource/acme/dana/blob.bin": payload}


def test_upload_reply_carries_url_and_object_key(store):
    doc = envelope(upload(store, "resource/acme/dana/notes.txt"))
    assert (doc["status"], doc["msg"]) == ("ok", ""), doc
    assert doc["data"] == "http://engines:4566/resource/acme/dana/notes.txt"
    assert doc["data2"] == "/resource/acme/dana/notes.txt"


def test_upload_records_resource(store):
    ok(upload(store, "resource/acme/dana/notes.txt", b"twelve bytes"))
    record = resources(store)["/resource/acme/dana/notes.txt"]
    expected = {"owner": "acme", "user": "dana", "provider": "files", "application": "app-acme", "tag": "custom", "parent": "",
                "fileName": "notes.txt", "fileType": "text", "fileFormat": ".txt", "url": "http://engines:4566/resource/acme/dana/notes.txt",
                "description": ""}
    assert {k: record[k] for k in expected} == expected
    assert is_count(record["fileSize"], 12)


def test_upload_keeps_description_parent_and_created_time(store):
    ok(upload(store, "resource/acme/dana/plan.txt", description="Q3 plan", parent="folder-7", createdTime="2021-02-03T04:05:06Z"))
    record = resources(store)["/resource/acme/dana/plan.txt"]
    assert (record["description"], record["parent"]) == ("Q3 plan", "folder-7")
    assert parse_time(record["createdTime"]) == parse_time("2021-02-03T04:05:06Z")


def test_repeated_path_gets_numbered_object_keys(store):
    replies = [envelope(upload(store, "resource/acme/dana/notes.txt", body)) for body in (b"one", b"two", b"three")]
    assert [r["data2"] for r in replies] == ["/resource/acme/dana/notes.txt", "/resource/acme/dana/notes-1.txt", "/resource/acme/dana/notes-2.txt"]
    assert objects(store.bucket) == {"resource/acme/dana/notes.txt": b"one", "resource/acme/dana/notes-1.txt": b"two",
                                     "resource/acme/dana/notes-2.txt": b"three"}
    assert sorted(resources(store)) == ["/resource/acme/dana/notes-1.txt", "/resource/acme/dana/notes-2.txt", "/resource/acme/dana/notes.txt"]


def test_file_type_and_format_follow_the_file(store):
    files = {"pic.png": "image/png", "clip.mp4": "video/mp4", "doc.pdf": "application/octet-stream", "README": "text/plain"}
    for name, content_type in files.items():
        ok(upload(store, f"resource/acme/dana/{name}", content_type=content_type))
    got = {r["fileName"]: (r["fileType"], r["fileFormat"]) for r in resources(store).values()}
    assert got == {"pic.png": ("image", ".png"), "clip.mp4": ("video", ".mp4"), "doc.pdf": ("application", ".pdf"), "README": ("", "")}


def test_upload_outside_the_users_folder_is_refused(store):
    for path in ("resource/acme/erin/x.txt", "resource/globex/dana/x.txt", "x.txt", "files/acme/dana/x.txt"):
        refused(upload(store, path))
    assert objects(store.bucket) == {}
    assert stored_resources(store) == []


def test_active_content_files_are_refused(store):
    for name in ("page.html", "icon.svg", "app.JS"):
        refused(upload(store, f"resource/acme/dana/{name}"))
    assert objects(store.bucket) == {}


def test_upload_without_user_names_the_empty_field(store):
    error(upload(store, "resource/acme/dana/x.txt", user=""), "Username or fullFilePath is empty: username = , fullFilePath = resource/acme/dana/x.txt")
    assert objects(store.bucket) == {}


def test_parent_directory_segments_are_rejected(store):
    error(upload(store, "resource/acme/dana/../x.txt"), "the fullFilePath: resource/acme/dana/../x.txt is not allowed")
    assert objects(store.bucket) == {}


def test_avatar_upload_sets_the_users_avatar(store):
    url = ok(upload(store, "avatar/acme/erin.png", b"\x89PNG", "image/png", user="erin", tag="avatar"))
    assert url == "http://engines:4566/avatar/acme/erin.png"
    assert store.user("acme", "erin")["avatar"] == url
    assert store.sql('SELECT avatar FROM "user" WHERE owner = %s AND name = %s', "acme", "erin") == [(url,)]
    assert objects(store.bucket) == {"avatar/acme/erin.png": b"\x89PNG"}


def test_avatar_must_be_an_image_named_after_its_user(store):
    refused(upload(store, "avatar/acme/erin.png", content_type="image/png", user="dana", tag="avatar"))
    refused(upload(store, "avatar/acme/dana.txt", user="dana", tag="avatar"))
    assert objects(store.bucket) == {}
    assert store.user("acme", "dana")["avatar"] == ""


def test_named_provider_applies_path_prefix_and_domain(store):
    doc = envelope(upload(store, "resource/acme/dana/a.txt", b"cdn", provider="files-cdn"))
    assert (doc["data"], doc["data2"]) == (CDN + "tenant-files/resource/acme/dana/a.txt", "/tenant-files/resource/acme/dana/a.txt")
    assert objects(store.bucket) == {"tenant-files/resource/acme/dana/a.txt": b"cdn"}
    assert resources(store)["/tenant-files/resource/acme/dana/a.txt"]["provider"] == "files-cdn"


def test_unknown_provider_is_named_in_the_error(store):
    error(upload(store, "resource/acme/dana/a.txt", provider="no-such"), "The provider: no-such is not found")


def test_application_without_storage_provider_cannot_upload(store):
    error(upload(store, "resource/globex/gus/a.txt", owner="globex", user="gus", cred="globex"),
          "No provider for category: Storage is found for application: app-globex")
    assert objects(store.bucket) == {}


def test_organization_owned_provider_may_not_reach_a_private_endpoint(store):
    error(upload(store, "resource/acme/dana/a.txt", provider="files-acme"), "the host: engines is not allowed, only Internet addresses can be requested")
    assert objects(store.bucket) == {}


def test_delete_resource_removes_object_and_record(store):
    ok(upload(store, "resource/acme/dana/keep.txt", b"keep"))
    ok(upload(store, "resource/acme/dana/drop.txt", b"drop"))
    ok(store.post("/api/delete-resource", {"owner": "acme", "name": "/resource/acme/dana/drop.txt"}), "Affected")
    assert objects(store.bucket) == {"resource/acme/dana/keep.txt": b"keep"}
    assert sorted(resources(store)) == ["/resource/acme/dana/keep.txt"]


def test_delete_resource_acts_on_the_stored_record(store):
    ok(upload(store, "resource/acme/dana/cdn.txt", b"x", provider="files-cdn"))
    body = {"owner": "acme", "name": "/tenant-files/resource/acme/dana/cdn.txt", "provider": "files", "user": "erin"}
    ok(store.post("/api/delete-resource", body), "Affected")
    assert objects(store.bucket) == {}
    assert resources(store) == {}


def test_delete_unknown_resource_is_refused(store):
    ok(upload(store, "resource/acme/dana/keep.txt", b"keep"))
    before = stored_resources(store)
    refused(store.post("/api/delete-resource", {"owner": "acme", "name": "/resource/acme/dana/none.txt"}))
    assert stored_resources(store) == before
    assert objects(store.bucket) == {"resource/acme/dana/keep.txt": b"keep"}


def test_resource_listing_paginates_with_total(store):
    for i in range(5):
        ok(upload(store, f"resource/acme/dana/f{i}.txt", createdTime=f"2021-01-0{i + 1}T00:00:00Z"))
    doc = envelope(store.get("/api/get-resources?owner=acme&user=dana&p=1&pageSize=2"))
    assert doc["status"] == "ok" and is_count(doc["data2"], 5), doc
    assert sorted(r["name"] for r in doc["data"]) == ["/resource/acme/dana/f3.txt", "/resource/acme/dana/f4.txt"]


def test_other_organization_cannot_list_resources(store):
    ok(upload(store, "resource/acme/dana/a.txt"))
    refused(store.get("/api/get-resources?owner=acme&user=dana", "globex"))
    assert ok(store.get("/api/get-resources?owner=globex&user=gus", "globex")) == []


@pytest.mark.timeout(150)
def test_resources_and_objects_survive_restart(store):
    ok(upload(store, "resource/acme/dana/a.txt", b"persist"))
    before = stored_resources(store)
    store.stop()
    store.start()
    assert stored_resources(store) == before
    assert list(resources(store)) == ["/resource/acme/dana/a.txt"]
    ok(store.post("/api/delete-resource", {"owner": "acme", "name": "/resource/acme/dana/a.txt"}), "Affected")
    assert objects(store.bucket) == {}
