"""Single-owner upload and file operations: creation, storage layout, downloads, removal and upload options."""

import time

from plikenv import basic, boolean, check_file, check_upload_flags, empty, error, number, ok


def upload_row(plik, upload_id):
    rows = plik.sql(
        'SELECT "user", coalesce(token, \'\'), upload_token, removable, deleted_at IS NOT NULL FROM uploads WHERE id = %s', upload_id
    )
    assert len(rows) == 1, rows
    return rows[0]


def file_rows(plik, upload_id):
    return sorted(plik.sql("SELECT id, name, size, status FROM files WHERE upload_id = %s", upload_id))


def test_create_upload_with_token_and_session(open_pair, plik):
    alice, _ = open_pair
    resp = alice.api.post("/upload", {"removable": False, "comments": "hello"})
    assert resp.status == 200, resp
    up = resp.json()
    assert isinstance(up["id"], str) and up["id"] and up["uploadToken"], up
    check_upload_flags(up, admin=True)
    assert up["comments"] == "hello"
    number(up["ttl"], 2592000)
    empty(up.get("files"))
    assert upload_row(plik, up["id"]) == ("local:alice", alice.token, up["uploadToken"], False, False)

    resp = alice.session.post("/upload", {})
    assert resp.status == 200, resp
    up2 = resp.json()
    check_upload_flags(up2, admin=True)
    assert upload_row(plik, up2["id"]) == ("local:alice", "", up2["uploadToken"], False, False)
    assert up2["id"] != up["id"] and up2["uploadToken"] != up["uploadToken"]


def test_unauthenticated_and_invalid_token_uploads_are_refused(open_pair, plik):
    error(plik.client().post("/upload", {}), 400, "unable to create upload : anonymous uploads are disabled")
    error(plik.client("plik_notarealtoken").post("/upload", {}), 403, "invalid token")
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_add_file_stores_metadata_and_object(open_pair, plik):
    alice, _ = open_pair
    up = alice.create_upload()
    data = b"alpha tenant bytes\n"
    resp = alice.api.add_file(up["id"], "notes.txt", data)
    assert resp.status == 200, resp
    f = resp.json()
    check_file(f, "notes.txt", data)
    binary = bytes(range(90))
    resp = alice.api.add_file(up["id"], "blob.bin", binary)
    assert resp.status == 200, resp
    g = resp.json()
    check_file(g, "blob.bin", binary, file_type="application/octet-stream")
    assert file_rows(plik, up["id"]) == sorted([(f["id"], "notes.txt", len(data), "uploaded"), (g["id"], "blob.bin", 90, "uploaded")])
    assert plik.objects() == {plik.key(up["id"], f["id"]): data, plik.key(up["id"], g["id"]): binary}


def test_public_download_and_head(open_pair, plik):
    alice, bobby = open_pair
    data = b"shared via link\n"
    up, f = alice.upload("link.txt", data)
    path = f"/file/{up['id']}/{f['id']}/link.txt"
    for caller in (plik.client(), bobby.api, bobby.session):
        resp = caller.get(path)
        assert resp.status == 200 and resp.body == data, resp
        assert resp.headers["Content-Type"] == "text/plain; charset=utf-8"
    head = plik.client().request("HEAD", path)
    assert head.status == 200 and head.body == b"", head
    assert head.headers["Content-Length"] == str(len(data))
    error(plik.client().get(f"/file/{up['id']}/{f['id']}/other.txt"), 400, "invalid file name")
    other = alice.create_upload()
    error(plik.client().get(f"/file/{other['id']}/{f['id']}/link.txt"), 404, f"file {f['id']} not found")
    error(plik.client().get(f"/file/nosuchupload/{f['id']}/link.txt"), 404, "upload nosuchupload not found")


def test_get_upload_reports_admin_only_to_managing_credentials(open_pair, plik):
    alice, bobby = open_pair
    up, f = alice.upload("a.txt", b"aaa")
    second_token = plik.client(alice.new_token("second"))
    for caller, admin in (
        (alice.api, True),
        (alice.session, True),
        (second_token, False),
        (bobby.api, False),
        (bobby.session, False),
        (plik.client(), False),
    ):
        resp = caller.get(f"/upload/{up['id']}")
        assert resp.status == 200, resp
        body = resp.json()
        assert body["id"] == up["id"]
        boolean(body["admin"], admin)
        if admin:
            assert body["uploadToken"] == up["uploadToken"]
        else:
            empty(body.get("uploadToken"))
        empty(body.get("user"))
        empty(body.get("token"))
        assert [x["id"] for x in body["files"]] == [f["id"]]
        check_file(body["files"][0], "a.txt", b"aaa")
    error(plik.client().get("/upload/doesnotexist"), 404, "upload doesnotexist not found")


def test_predeclared_files_are_filled_in_place(open_pair, plik):
    alice, bobby = open_pair
    resp = alice.api.post("/upload", {"files": [{"fileName": "pre.txt"}, {"fileName": "later.txt"}]})
    assert resp.status == 200, resp
    up = resp.json()
    declared = {x["fileName"]: x for x in up["files"]}
    assert sorted(declared) == ["later.txt", "pre.txt"]
    for x in declared.values():
        assert x["status"] == "missing", x
    pre, later = declared["pre.txt"]["id"], declared["later.txt"]["id"]
    assert file_rows(plik, up["id"]) == sorted([(pre, "pre.txt", 0, "missing"), (later, "later.txt", 0, "missing")])

    error(alice.api.add_file(up["id"], "wrong.txt", b"x", suffix=f"/{pre}/pre.txt"), 400, "invalid file name")
    error(bobby.api.add_file(up["id"], "pre.txt", b"bob", suffix=f"/{pre}/pre.txt"), 403, "you are not allowed to add file to this upload")
    assert file_rows(plik, up["id"]) == sorted([(pre, "pre.txt", 0, "missing"), (later, "later.txt", 0, "missing")])

    resp = alice.api.add_file(up["id"], "pre.txt", b"pre", suffix=f"/{pre}/pre.txt")
    assert resp.status == 200, resp
    check_file(resp.json(), "pre.txt", b"pre")
    assert resp.json()["id"] == pre
    error(alice.api.add_file(up["id"], "pre.txt", b"again", suffix=f"/{pre}/pre.txt"), 400, "invalid file status uploaded, expected missing")
    assert file_rows(plik, up["id"]) == sorted([(pre, "pre.txt", 3, "uploaded"), (later, "later.txt", 0, "missing")])
    assert plik.objects() == {plik.key(up["id"], pre): b"pre"}
    error(plik.client().get(f"/file/{up['id']}/{later}/later.txt"), 404, f"file later.txt ({later}) is not available : missing")


def test_owner_removes_one_file(open_pair, plik):
    alice, bobby = open_pair
    up = alice.create_upload()
    keep = alice.api.add_file(up["id"], "keep.txt", b"keep me").json()
    drop = alice.api.add_file(up["id"], "drop.txt", b"drop me").json()
    path = f"/file/{up['id']}/{drop['id']}/drop.txt"
    ok(alice.api.delete(path))
    assert file_rows(plik, up["id"]) == sorted([(keep["id"], "keep.txt", 7, "uploaded"), (drop["id"], "drop.txt", 7, "deleted")])
    assert plik.objects() == {plik.key(up["id"], keep["id"]): b"keep me"}
    error(plik.client().get(path), 404, f"file drop.txt ({drop['id']}) is not available : deleted")
    assert plik.client().get(f"/file/{up['id']}/{keep['id']}/keep.txt").body == b"keep me"
    body = alice.api.get(f"/upload/{up['id']}").json()
    assert {x["id"]: x["status"] for x in body["files"]} == {keep["id"]: "uploaded", drop["id"]: "deleted"}


def test_owner_deletes_upload_softly(open_pair, plik):
    alice, _ = open_pair
    up, f = alice.upload("gone.txt", b"soon gone")
    ok(alice.api.delete(f"/upload/{up['id']}"))
    assert upload_row(plik, up["id"])[4] is True
    error(alice.api.get(f"/upload/{up['id']}"), 404, f"upload {up['id']} not found")
    error(plik.client().get(f"/file/{up['id']}/{f['id']}/gone.txt"), 404, f"upload {up['id']} not found")
    error(alice.api.delete(f"/upload/{up['id']}"), 404, f"upload {up['id']} not found")
    assert plik.sql("SELECT status FROM files WHERE id = %s", f["id"]) == [("removed",)]
    # Session owner can delete an upload made with the session.
    up2 = alice.create_upload(alice.session)
    ok(alice.session.delete(f"/upload/{up2['id']}"))
    assert upload_row(plik, up2["id"])[4] is True


def test_password_protected_downloads(open_pair, plik):
    alice, bobby = open_pair
    resp = bobby.api.post("/upload", {"protectedByPassword": True, "login": "share", "password": "s3cret"})
    assert resp.status == 200, resp
    up = resp.json()
    check_upload_flags(up, protected=True, admin=True)
    f = bobby.api.add_file(up["id"], "p.txt", b"protected!").json()
    path = f"/file/{up['id']}/{f['id']}/p.txt"
    prefix = "please provide valid credentials to access this upload : "
    resp = plik.client().get(path)
    error(resp, 401, prefix + "missing Authorization header")
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="plik"'
    error(plik.client().get(path, headers=basic("share", "wrong")), 401, prefix + "invalid credentials")
    error(plik.client().get(path, headers=basic("other", "s3cret")), 401, prefix + "invalid credentials")
    error(alice.api.get(path), 401, prefix + "missing Authorization header")
    error(plik.client().get(f"/upload/{up['id']}"), 401, prefix + "missing Authorization header")
    for caller, headers in ((plik.client(), basic("share", "s3cret")), (bobby.api, None), (bobby.session, None)):
        resp = caller.get(path, headers=headers)
        assert resp.status == 200 and resp.body == b"protected!", resp
    body = plik.client().get(f"/upload/{up['id']}", headers=basic("share", "s3cret")).json()
    boolean(body["admin"], False)
    boolean(body["protectedByPassword"], True)
    error(bobby.api.post("/upload", {"protectedByPassword": True}), 400, "unable to create upload : upload password is empty")
    # Without a login the share login defaults to "plik".
    resp = bobby.api.post("/upload", {"protectedByPassword": True, "password": "pp-only"})
    assert resp.status == 200, resp
    up2 = resp.json()
    f2 = bobby.api.add_file(up2["id"], "d.txt", b"default login").json()
    path2 = f"/file/{up2['id']}/{f2['id']}/d.txt"
    error(plik.client().get(path2, headers=basic("share", "pp-only")), 401, prefix + "invalid credentials")
    resp = plik.client().get(path2, headers=basic("plik", "pp-only"))
    assert resp.status == 200 and resp.body == b"default login", resp


def test_one_shot_file_is_served_once(open_pair, plik):
    alice, _ = open_pair
    resp = alice.api.post("/upload", {"oneShot": True})
    up = resp.json()
    check_upload_flags(up, one_shot=True, admin=True)
    f = alice.api.add_file(up["id"], "once.txt", b"only once").json()
    path = f"/file/{up['id']}/{f['id']}/once.txt"
    assert plik.client().request("HEAD", path).status == 200
    resp = plik.client().get(path)
    assert resp.status == 200 and resp.body == b"only once", resp
    error(plik.client().get(path), 404, f"file once.txt ({f['id']}) is not available : deleted")
    deadline = time.time() + 10
    while plik.sql("SELECT status FROM files WHERE id = %s", f["id"]) != [("deleted",)] and time.time() < deadline:
        time.sleep(0.2)
    assert plik.sql("SELECT status FROM files WHERE id = %s", f["id"]) == [("deleted",)]


def test_quick_upload_returns_link(open_pair, plik):
    _, bobby = open_pair
    boundary = "----quick"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="quick.txt"\r\n\r\n').encode()
    body += b"quick bytes" + f"\r\n--{boundary}--\r\n".encode()
    resp = bobby.api.post("/", body, ctype=f"multipart/form-data; boundary={boundary}")
    assert resp.status == 200, resp
    rows = plik.sql('SELECT u.id, u."user", u.token, f.id, f.size, f.status FROM uploads u JOIN files f ON f.upload_id = u.id')
    assert len(rows) == 1, rows
    upload_id, user, token, file_id, size, status = rows[0]
    assert (user, token, size, status) == ("local:bobby", bobby.token, 11, "uploaded")
    assert resp.text.rstrip() == f"{plik.base}/file/{upload_id}/{file_id}/quick.txt", resp
    assert plik.objects() == {plik.key(upload_id, file_id): b"quick bytes"}
    error(plik.client().post("/", body, ctype=f"multipart/form-data; boundary={boundary}"), 400,
          "unable to create upload : anonymous uploads are disabled")
    assert plik.sql("SELECT count(*) FROM uploads") == [(1,)]

