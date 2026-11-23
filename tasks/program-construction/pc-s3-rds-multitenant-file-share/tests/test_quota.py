"""Per-user size accounting: limits, boundaries, failure effects and interactions with deletion and the CLI."""

from plikenv import counts, error, ok

FULL_64 = "maximum user upload size reached. (64 B)"


def uploaded_bytes(plik):
    rows = plik.sql(
        'SELECT u."user", sum(f.size) FROM uploads u JOIN files f ON f.upload_id = u.id '
        "WHERE f.status = 'uploaded' AND u.deleted_at IS NULL GROUP BY u.\"user\""
    )
    return {user: int(total) for user, total in rows}


def test_limit_is_per_user_and_inclusive(pair, plik):
    alice, bobby = pair
    ua = alice.create_upload()
    assert alice.api.add_file(ua["id"], "f40.txt", b"a" * 40).status == 200
    assert alice.api.add_file(ua["id"], "f24.txt", b"b" * 24).status == 200  # exactly 64
    ub = bobby.create_upload()
    assert bobby.api.add_file(ub["id"], "b29.txt", b"c" * 29).status == 200
    before_objects = plik.objects()

    resp = alice.api.add_file(ua["id"], "f1.txt", b"x")
    error(resp, 400, FULL_64)
    rejected = plik.sql("SELECT size, status FROM files WHERE upload_id = %s AND name = 'f1.txt'", ua["id"])
    assert rejected == [(1, "deleted")], rejected
    assert plik.objects() == before_objects
    counts(alice.session.get("/me/stats").json(), {"uploads": 1, "files": 2, "totalSize": 64})

    # Bobby is unaffected by Alice being full.
    resp = bobby.api.add_file(ub["id"], "b30.txt", b"d" * 30)
    assert resp.status == 200, resp
    counts(bobby.session.get("/me/stats").json(), {"uploads": 1, "files": 2, "totalSize": 59})
    assert uploaded_bytes(plik) == {alice.id: 64, bobby.id: 59}
    error(bobby.api.add_file(ub["id"], "b6.txt", b"e" * 6), 400, FULL_64)
    assert uploaded_bytes(plik) == {alice.id: 64, bobby.id: 59}


def test_single_oversized_file_is_rejected_and_purged(pair, plik):
    alice, bobby = pair
    up = bobby.create_upload()
    error(bobby.api.add_file(up["id"], "big.txt", b"z" * 65), 400, FULL_64)
    assert plik.sql("SELECT size, status FROM files WHERE upload_id = %s", up["id"]) == [(65, "deleted")]
    assert plik.objects() == {}
    # The empty upload created beforehand remains and counts as an upload, not as bytes.
    counts(bobby.session.get("/me/stats").json(), {"uploads": 1, "files": 0, "totalSize": 0})
    assert bobby.api.add_file(up["id"], "fits.txt", b"y" * 64).status == 200
    assert uploaded_bytes(plik) == {bobby.id: 64}
    a_up, _ = alice.upload("alice.txt", b"a" * 64)
    assert uploaded_bytes(plik) == {alice.id: 64, bobby.id: 64}


def test_declared_sizes_are_checked_at_creation(pair, plik):
    alice, bobby = pair
    error(bobby.api.post("/upload", {"files": [{"fileName": "z.txt", "fileSize": 65}]}), 400, "unable to create upload : " + FULL_64)
    resp = bobby.api.post("/upload", {"files": [{"fileName": "z.txt", "fileSize": 30}]})
    assert resp.status == 200, resp
    alice.upload("a.txt", b"a" * 60)
    error(alice.api.post("/upload", {"files": [{"fileName": "z.txt", "fileSize": 5}]}), 400, "unable to create upload : " + FULL_64)
    resp = alice.api.post("/upload", {"files": [{"fileName": "z.txt", "fileSize": 4}]})
    assert resp.status == 200, resp
    assert plik.sql('SELECT count(*) FROM uploads WHERE "user" = %s', alice.id) == [(2,)]
    assert plik.sql('SELECT count(*) FROM uploads WHERE "user" = %s', bobby.id) == [(1,)]


def test_deleting_data_frees_quota(pair, plik):
    alice, _ = pair
    up, f = alice.upload("first.txt", b"f" * 50)
    error(alice.api.add_file(up["id"], "more.txt", b"m" * 20), 400, FULL_64)
    ok(alice.api.delete(f"/file/{up['id']}/{f['id']}/first.txt"))
    counts(alice.session.get("/me/stats").json(), {"uploads": 1, "files": 0, "totalSize": 0})
    resp = alice.api.add_file(up["id"], "more.txt", b"m" * 60)
    assert resp.status == 200, resp
    ok(alice.api.delete(f"/upload/{up['id']}"))
    counts(alice.session.get("/me/stats").json(), {"uploads": 0, "files": 0, "totalSize": 0})
    again, _ = alice.upload("again.txt", b"g" * 64)
    assert uploaded_bytes(plik) == {alice.id: 64}


def test_capability_uploads_count_for_the_upload_owner(pair, plik):
    alice, bobby = pair
    ub = bobby.create_upload()
    capability = {"X-UploadToken": ub["uploadToken"]}
    assert plik.client().add_file(ub["id"], "guest.txt", b"g" * 40, headers=capability).status == 200
    counts(bobby.session.get("/me/stats").json(), {"uploads": 1, "files": 1, "totalSize": 40})
    counts(alice.session.get("/me/stats").json(), {"uploads": 0, "files": 0, "totalSize": 0})
    # Bobby now only has 24 bytes left; Alice still has her full allowance.
    error(bobby.api.add_file(ub["id"], "b25.txt", b"b" * 25), 400, FULL_64)
    alice.upload("a64.txt", b"a" * 64)
    assert uploaded_bytes(plik) == {alice.id: 64, bobby.id: 40}


def test_server_default_applies_without_user_limit(plik):
    plik.write_config(max_user_size="100B")
    plik.add_user("alice", "alice-secret-1")
    plik.add_user("bobby", "bobby-secret-2", "-1")
    plik.start()
    alice, bobby = plik.tenant("alice", "alice-secret-1"), plik.tenant("bobby", "bobby-secret-2")
    up, _ = alice.upload("ninety.bin", bytes(range(90)))
    resp = alice.api.add_file(up["id"], "eleven.txt", b"x" * 11)
    assert resp.status == 400 and resp.text.startswith("maximum user upload size reached."), resp
    assert alice.api.add_file(up["id"], "ten.txt", b"x" * 10).status == 200
    bobby.upload("big.txt", b"y" * 500)
    assert uploaded_bytes(plik) == {alice.id: 100, bobby.id: 500}


def test_cli_limit_change_applies_to_running_server(pair, plik):
    alice, bobby = pair
    alice.upload("sixty.txt", b"s" * 60)
    updated = plik.cli("user", "update", "--login", "alice", "--max-user-size", "10B")
    assert updated.returncode == 0, updated
    error(alice.api.post("/upload", {}), 400, "unable to create upload : maximum user upload size reached. (10 B)")
    assert plik.sql('SELECT count(*) FROM uploads WHERE "user" = %s', alice.id) == [(1,)]
    bobby.upload("bobby.txt", b"b" * 64)
    updated = plik.cli("user", "update", "--login", "alice", "--max-user-size", "-1")
    assert updated.returncode == 0, updated
    alice.upload("large.txt", b"l" * 500)
    assert uploaded_bytes(plik) == {alice.id: 560, bobby.id: 64}
