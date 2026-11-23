"""Offline file and user maintenance through the CLI: file list/show/delete, user delete, metadata export/import."""

import json
import uuid

import psycopg

from harness import run_program, write_fixture
from plikenv import solo


def stored(alice):
    up = alice.create_upload()
    f = alice.api.add_file(up["id"], "notes.txt", b"eleven char").json()
    g = alice.api.add_file(up["id"], "blob.bin", bytes(range(50))).json()
    return up, f, g


def test_file_list_prints_one_line_per_file(plik):
    alice = solo(plik)
    up, f, g = stored(alice)
    result = plik.cli("file", "list", "--human=false")
    assert result.returncode == 0, result
    assert sorted(result.stdout.splitlines()) == sorted([
        f"{up['id']} {f['id']} 11 uploaded text/plain; charset=utf-8 notes.txt",
        f"{up['id']} {g['id']} 50 uploaded application/octet-stream blob.bin",
    ])


def test_file_list_uses_human_sizes_by_default(plik):
    alice = solo(plik)
    up, f, _ = stored(alice)
    result = plik.cli("file", "list", "--file", f["id"])
    assert result.returncode == 0, result
    assert result.stdout.splitlines() == [f"{up['id']} {f['id']} 11 B uploaded text/plain; charset=utf-8 notes.txt"]


def test_file_list_filters_by_upload(plik):
    alice = solo(plik)
    up, f, g = stored(alice)
    other, h = alice.upload("other.txt", b"other")
    result = plik.cli("file", "list", "--human=false", "--upload", other["id"])
    assert result.returncode == 0, result
    assert result.stdout.splitlines() == [f"{other['id']} {h['id']} 5 uploaded text/plain; charset=utf-8 other.txt"]


def test_file_show_prints_the_file_record(plik):
    alice = solo(plik)
    _, f, _ = stored(alice)
    result = plik.cli("file", "show", "--file", f["id"])
    assert result.returncode == 0, result
    record = json.loads(result.stdout[: result.stdout.rindex("}") + 1])
    assert record["id"] == f["id"] and record["fileName"] == "notes.txt" and record["status"] == "uploaded"
    assert record["fileSize"] == 11 and record["fileMd5"] == f["fileMd5"]


def test_file_show_of_unknown_file_fails(plik):
    plik.add_user("alice", "alice-secret-1")
    result = plik.cli("file", "show", "--file", "nosuchfile")
    assert result.returncode != 0
    assert result.stdout.rstrip() == "File nosuchfile not found"


def test_file_delete_requires_exactly_one_selector(plik):
    plik.add_user("alice", "alice-secret-1")
    assert plik.cli("file", "delete").returncode != 0
    assert plik.cli("file", "delete", "--file", "a", "--upload", "b").returncode != 0


def test_confirmed_file_delete_removes_the_file_and_its_object(plik):
    alice = solo(plik)
    up, f, g = stored(alice)
    result = plik.cli("file", "delete", "--file", f["id"], stdin="y\n")
    assert result.returncode == 0, result
    assert sorted(plik.sql("SELECT id, status FROM files")) == sorted([(f["id"], "deleted"), (g["id"], "uploaded")])
    assert set(plik.objects()) == {plik.key(up["id"], g["id"])}


def test_declined_file_delete_changes_nothing(plik):
    alice = solo(plik)
    up, f, g = stored(alice)
    result = plik.cli("file", "delete", "--file", f["id"], stdin="n\n")
    assert result.returncode == 0, result
    assert sorted(plik.sql("SELECT status FROM files")) == [("uploaded",), ("uploaded",)]
    assert set(plik.objects()) == {plik.key(up["id"], f["id"]), plik.key(up["id"], g["id"])}


def test_confirmed_upload_delete_purges_the_upload(plik):
    alice = solo(plik)
    up, _, _ = stored(alice)
    keep, k = alice.upload("keep.txt", b"keep")
    result = plik.cli("file", "delete", "--upload", up["id"], stdin="y\n")
    assert result.returncode == 0, result
    assert plik.sql("SELECT id FROM uploads") == [(keep["id"],)]
    assert plik.objects() == {plik.key(keep["id"], k["id"]): b"keep"}


def test_confirmed_delete_all_removes_every_upload(plik):
    alice = solo(plik)
    stored(alice)
    alice.upload("more.txt", b"more")
    result = plik.cli("file", "delete", "--all", stdin="y\n")
    assert result.returncode == 0, result
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]
    assert plik.sql("SELECT count(*) FROM files") == [(0,)]
    assert plik.objects() == {}


def test_confirmed_user_delete_removes_user_tokens_uploads_and_objects(plik):
    plik.add_user("bobby", "bobby-secret-2", "-1")
    alice = solo(plik)
    bobby = plik.tenant("bobby", "bobby-secret-2")
    stored(alice)
    bup, bf = bobby.upload("b.txt", b"bobby")
    result = plik.cli("user", "delete", "--login", "alice", stdin="y\n")
    assert result.returncode == 0, result
    assert plik.sql("SELECT id FROM users") == [("local:bobby",)]
    assert plik.sql("SELECT user_id FROM tokens") == [("local:bobby",)]
    assert plik.sql('SELECT "user" FROM uploads') == [("local:bobby",)]
    assert plik.objects() == {plik.key(bup["id"], bf["id"]): b"bobby"}


def test_unconfirmed_user_delete_fails_and_keeps_the_user(plik):
    plik.add_user("alice", "alice-secret-1")
    result = plik.cli("user", "delete", "--login", "alice")
    assert result.returncode != 0
    assert plik.sql("SELECT id FROM users") == [("local:alice",)]


def second_database(plik):
    """A second empty database in the same engine and a configuration file pointing at it."""
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(plik.engine["dsn"] + " dbname=postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    config = plik.dir / "second.toml"
    write_fixture(config, plik.config.read_text().replace(f"dbname={plik.dbname}", f"dbname={name}"), mode=0o644)
    return name, config


def drop_database(plik, name):
    with psycopg.connect(plik.engine["dsn"] + " dbname=postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def snapshot(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return (
            sorted(conn.execute("SELECT id, login, max_user_size, is_admin FROM users").fetchall()),
            sorted(conn.execute("SELECT token, user_id, comment FROM tokens").fetchall()),
            sorted(conn.execute('SELECT id, "user", token, upload_token, ttl FROM uploads').fetchall()),
            sorted(conn.execute("SELECT id, upload_id, name, size, status, md5 FROM files").fetchall()),
        )


def test_export_then_import_copies_the_metadata(plik):
    alice = solo(plik, limit="5KB")
    stored(alice)
    alice.new_token("spare")
    export = plik.dir / "metadata.bin"
    result = plik.cli("export", str(export))
    assert result.returncode == 0, result
    name, config = second_database(plik)
    try:
        result = run_program(["--config", config, "import", str(export)], cwd=plik.dir, timeout=45)
        assert result.returncode == 0, result
        assert snapshot(f"{plik.engine['dsn']} dbname={name}") == snapshot(plik.dsn)
    finally:
        drop_database(plik, name)


def test_import_into_a_populated_database_fails(plik):
    alice = solo(plik)
    stored(alice)
    export = plik.dir / "metadata.bin"
    assert plik.cli("export", str(export)).returncode == 0
    result = plik.cli("import", str(export))
    assert result.returncode != 0
