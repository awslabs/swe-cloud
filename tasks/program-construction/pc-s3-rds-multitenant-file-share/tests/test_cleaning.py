"""The `clean` command: expiring uploads past their TTL, deleting removed files' S3 objects and purging metadata."""

from plikenv import page, solo, wait_until


def clean(plik):
    result = plik.cli("clean")
    assert result.returncode == 0, result
    return result


def expired(plik, client, upload_id):
    """Wait until the server reports the upload as gone (its TTL has passed)."""
    return wait_until(lambda: client.get(f"/upload/{upload_id}").status == 404)


def test_clean_deletes_objects_of_removed_uploads(plik):
    alice = solo(plik)
    gone, f = alice.upload("gone.txt", b"to be cleaned")
    keep, g = alice.upload("keep.txt", b"stays")
    assert alice.api.delete(f"/upload/{gone['id']}").status == 200
    assert set(plik.objects()) == {plik.key(gone["id"], f["id"]), plik.key(keep["id"], g["id"])}
    clean(plik)
    assert plik.objects() == {plik.key(keep["id"], g["id"]): b"stays"}


def test_clean_purges_rows_of_removed_uploads(plik):
    alice = solo(plik)
    gone, f = alice.upload("gone.txt", b"to be cleaned")
    keep, g = alice.upload("keep.txt", b"stays")
    assert alice.api.delete(f"/upload/{gone['id']}").status == 200
    clean(plik)
    assert plik.sql("SELECT id FROM uploads") == [(keep["id"],)]
    assert plik.sql("SELECT id, status FROM files") == [(g["id"], "uploaded")]


def test_clean_expires_uploads_past_their_ttl(plik):
    alice = solo(plik)
    short, f = alice.upload("short.txt", b"short lived", ttl=1)
    keep, g = alice.upload("keep.txt", b"stays")
    assert expired(plik, alice.api, short["id"])
    clean(plik)
    assert plik.sql("SELECT id FROM uploads") == [(keep["id"],)]
    assert plik.sql("SELECT id FROM files") == [(g["id"],)]
    assert plik.objects() == {plik.key(keep["id"], g["id"]): b"stays"}


def test_clean_leaves_live_uploads_untouched(plik):
    alice = solo(plik)
    up = alice.create_upload(ttl=3600)
    f = alice.api.add_file(up["id"], "live.txt", b"live bytes").json()
    clean(plik)
    assert plik.sql('SELECT id, "user", deleted_at FROM uploads') == [(up["id"], alice.id, None)]
    assert plik.sql("SELECT id, status FROM files") == [(f["id"], "uploaded")]
    assert plik.objects() == {plik.key(up["id"], f["id"]): b"live bytes"}
    resp = plik.client().get(f"/file/{up['id']}/{f['id']}/live.txt")
    assert resp.status == 200 and resp.body == b"live bytes"


def test_clean_with_nothing_to_do_succeeds(plik):
    plik.add_user("alice", "alice-secret-1")
    clean(plik)
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_expired_uploads_stay_listed_until_cleaned(plik):
    alice = solo(plik)
    short, _ = alice.upload("short.txt", b"short lived", ttl=1)
    keep, _ = alice.upload("keep.txt", b"stays")
    assert expired(plik, alice.api, short["id"])
    listed = page(alice.session.get("/me/uploads"))
    assert sorted(x["id"] for x in listed["results"]) == sorted([short["id"], keep["id"]])
    clean(plik)
    listed = page(alice.session.get("/me/uploads"))
    assert [x["id"] for x in listed["results"]] == [keep["id"]]


def test_clean_removes_objects_of_files_consumed_by_a_one_shot_archive(plik):
    alice = solo(plik)
    up = alice.create_upload(oneShot=True)
    f = alice.api.add_file(up["id"], "a.txt", b"one shot a").json()
    g = alice.api.add_file(up["id"], "b.txt", b"one shot b").json()
    assert plik.client().get(f"/archive/{up['id']}/all.zip").status == 200
    clean(plik)
    assert plik.objects() == {}
    assert sorted(plik.sql("SELECT id, status FROM files")) == sorted([(f["id"], "deleted"), (g["id"], "deleted")])
    assert plik.sql("SELECT id, deleted_at FROM uploads") == [(up["id"], None)]
