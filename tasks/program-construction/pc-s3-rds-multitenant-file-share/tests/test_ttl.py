"""Upload lifetime: default and maximum TTL, per-user maximum, infinite TTL, expiry replies and TTL extension."""

import time

from plikenv import error, number, solo, wait_until


def lifetime(plik, upload_id):
    """Seconds between an upload's creation and its recorded expiry (None when it never expires)."""
    ((created, expires),) = plik.sql("SELECT created_at, expire_at FROM uploads WHERE id = %s", upload_id)
    return None if expires is None else (expires - created).total_seconds()


def test_default_ttl_comes_from_configuration(plik):
    alice = solo(plik, extra='DefaultTTLStr = "10m"')
    up = alice.create_upload()
    number(up["ttl"], 600)
    assert plik.sql("SELECT ttl FROM uploads WHERE id = %s", up["id"]) == [(600,)]


def test_expiry_is_recorded_as_creation_plus_ttl(plik):
    alice = solo(plik)
    up = alice.create_upload(ttl=7200)
    assert abs(lifetime(plik, up["id"]) - 7200) < 1


def test_ttl_above_server_maximum_is_refused(plik):
    alice = solo(plik, extra='DefaultTTLStr = "1h"\nMaxTTLStr = "2h"')
    error(alice.api.post("/upload", {"ttl": 7201}), 400, "unable to create upload : invalid TTL. (maximum allowed is : 7200)")
    assert alice.api.post("/upload", {"ttl": 7200}).status == 200
    assert plik.sql("SELECT ttl FROM uploads") == [(7200,)]


def test_default_ttl_above_maximum_is_a_configuration_error(plik):
    plik.write_config(extra='DefaultTTLStr = "3h"\nMaxTTLStr = "2h"')
    result = plik.cli("user", "list")
    assert result.returncode != 0, result


def test_infinite_ttl_is_refused_under_a_maximum(plik):
    alice = solo(plik)
    error(alice.api.post("/upload", {"ttl": -1}), 400, "unable to create upload : cannot set infinite TTL (maximum allowed is : 2592000)")
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_infinite_ttl_is_allowed_without_a_maximum(plik):
    alice = solo(plik, extra='MaxTTLStr = "0"')
    up = alice.create_upload(ttl=-1)
    number(up["ttl"], -1)
    assert plik.sql("SELECT ttl, expire_at FROM uploads WHERE id = %s", up["id"]) == [(-1, None)]


def test_user_max_ttl_overrides_server_maximum(plik):
    result = plik.cli("user", "create", "--login", "alice", "--password", "alice-secret-1", "--max-ttl", "30m")
    assert result.returncode == 0, result
    plik.start()
    alice = plik.tenant("alice", "alice-secret-1")
    error(alice.api.post("/upload", {"ttl": 1801}), 400, "unable to create upload : invalid TTL. (maximum allowed is : 1800)")
    up = alice.create_upload(ttl=1800)
    number(up["ttl"], 1800)


def test_default_ttl_above_user_maximum_is_refused(plik):
    result = plik.cli("user", "create", "--login", "alice", "--password", "alice-secret-1", "--max-ttl", "1h")
    assert result.returncode == 0, result
    plik.start()
    alice = plik.tenant("alice", "alice-secret-1")
    error(alice.api.post("/upload", {}), 400, "unable to create upload : invalid TTL. (maximum allowed is : 3600)")


def test_disabled_ttl_choice_forces_the_default(plik):
    alice = solo(plik, extra='FeatureSetTTL = "disabled"\nDefaultTTLStr = "10m"')
    up = alice.create_upload(ttl=5)
    number(up["ttl"], 600)
    assert abs(lifetime(plik, up["id"]) - 600) < 1


def test_expired_upload_metadata_is_not_found(plik):
    alice = solo(plik)
    up = alice.create_upload(ttl=1)
    resp = wait_until(lambda: (r := alice.api.get(f"/upload/{up['id']}")).status == 404 and r)
    error(resp, 404, f"upload {up['id']} has expired")


def test_expired_upload_downloads_are_not_found(plik):
    alice = solo(plik)
    up, f = alice.upload("brief.txt", b"brief", ttl=1)
    path = f"/file/{up['id']}/{f['id']}/brief.txt"
    resp = wait_until(lambda: (r := plik.client().get(path)).status == 404 and r)
    error(resp, 404, f"upload {up['id']} has expired")


def test_ttl_extension_is_refused_when_not_enabled(plik):
    alice = solo(plik)
    error(alice.api.post("/upload", {"extend_ttl": True}), 400, "unable to create upload : extend TTL is disabled")
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_download_extends_expiry_when_ttl_extension_is_requested(plik):
    alice = solo(plik, extra='FeatureExtendTTL = "enabled"')
    up, f = alice.upload("ext.txt", b"extend me", ttl=100, extend_ttl=True)
    assert up["extend_ttl"] is True
    assert lifetime(plik, up["id"]) < 101
    time.sleep(3)
    assert plik.client().get(f"/file/{up['id']}/{f['id']}/ext.txt").status == 200
    assert lifetime(plik, up["id"]) >= 102
