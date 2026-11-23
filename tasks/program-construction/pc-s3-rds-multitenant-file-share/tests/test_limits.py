"""Per-file size limits (server and per user) and the per-upload file count limit."""

from plikenv import error, solo


def test_file_over_server_maximum_size_is_refused(plik):
    alice = solo(plik, extra='MaxFileSizeStr = "100B"')
    up = alice.create_upload()
    error(alice.api.add_file(up["id"], "big.txt", b"x" * 101), 400, "file too big (limit is set to 100 B)")


def test_file_of_exactly_maximum_size_is_accepted(plik):
    alice = solo(plik, extra='MaxFileSizeStr = "100B"')
    up = alice.create_upload()
    resp = alice.api.add_file(up["id"], "edge.txt", b"x" * 100)
    assert resp.status == 200, resp
    assert plik.sql("SELECT size, status FROM files") == [(100, "uploaded")]


def test_refused_oversized_file_leaves_a_deleted_row_and_no_object(plik):
    alice = solo(plik, extra='MaxFileSizeStr = "100B"')
    up = alice.create_upload()
    assert alice.api.add_file(up["id"], "big.txt", b"x" * 500).status == 400
    assert plik.sql("SELECT name, size, status FROM files") == [("big.txt", 0, "deleted")]
    assert plik.objects() == {}


def test_user_maximum_file_size_overrides_server_limit(plik):
    plik.write_config(extra='MaxFileSizeStr = "100B"')
    result = plik.cli("user", "create", "--login", "alice", "--password", "alice-secret-1", "--max-file-size", "10B")
    assert result.returncode == 0, result
    plik.start()
    alice = plik.tenant("alice", "alice-secret-1")
    up = alice.create_upload()
    error(alice.api.add_file(up["id"], "eleven.txt", b"x" * 11), 400, "file too big (limit is set to 10 B)")
    assert alice.api.add_file(up["id"], "ten.txt", b"x" * 10).status == 200


def test_declared_file_over_maximum_size_is_refused_at_creation(plik):
    alice = solo(plik, extra='MaxFileSizeStr = "100B"')
    error(
        alice.api.post("/upload", {"files": [{"fileName": "a.bin", "fileSize": 101}]}),
        400,
        "unable to create upload : file is too big (101 B), maximum file size is 100 B",
    )
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_declared_file_without_a_name_is_refused(plik):
    alice = solo(plik)
    error(alice.api.post("/upload", {"files": [{"fileSize": 1}]}), 400, "unable to create upload : missing file name")
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_file_count_limit_refuses_an_extra_file(plik):
    alice = solo(plik, extra="MaxFilePerUpload = 2")
    up = alice.create_upload()
    assert alice.api.add_file(up["id"], "one.txt", b"1").status == 200
    assert alice.api.add_file(up["id"], "two.txt", b"2").status == 200
    error(alice.api.add_file(up["id"], "three.txt", b"3"), 400, "maximum number file per upload reached, limit is 2")
    assert sorted(plik.sql("SELECT name FROM files")) == [("one.txt",), ("two.txt",)]


def test_refused_files_count_toward_the_file_limit(plik):
    alice = solo(plik, extra='MaxFilePerUpload = 2\nMaxFileSizeStr = "10B"')
    up = alice.create_upload()
    assert alice.api.add_file(up["id"], "big.txt", b"x" * 11).status == 400
    assert alice.api.add_file(up["id"], "one.txt", b"1").status == 200
    error(alice.api.add_file(up["id"], "two.txt", b"2"), 400, "maximum number file per upload reached, limit is 2")


def test_declaring_more_files_than_the_limit_is_refused(plik):
    alice = solo(plik, extra="MaxFilePerUpload = 2")
    files = [{"fileName": name} for name in ("a", "b", "c")]
    error(alice.api.post("/upload", {"files": files}), 400, "unable to create upload : too many files. maximum is 2")
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]
