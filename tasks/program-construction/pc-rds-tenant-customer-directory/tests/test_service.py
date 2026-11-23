"""Server invocation, database creation, init-data provisioning and persistence across restarts."""

import pytest

from support import (
    SEED_USERS,
    TENANT_INIT,
    TENANTS,
    admin_connect,
    assert_profile,
    assert_row,
    error,
    free_port,
    init_data,
    ok,
    refused,
)


def database_exists(d):
    with admin_connect(d.rds) as c:
        return bool(c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (d.db,)).fetchall())


def test_create_database_and_provision_tenants(unstarted):
    d = unstarted
    assert not database_exists(d)
    d.write_files(TENANT_INIT)
    d.start("-createDatabase")
    assert database_exists(d)
    apps = d.sql("SELECT name, organization, client_id, client_secret FROM application WHERE organization IN ('acme', 'globex') ORDER BY name")
    assert apps == [(t["app"], org, t["client_id"], t["secret"]) for org, t in sorted(TENANTS.items())]
    orgs = d.sql("SELECT owner, name FROM organization WHERE name IN ('acme', 'globex') ORDER BY name")
    assert orgs == [("admin", "acme"), ("admin", "globex")]
    assert d.sql('SELECT owner, name FROM "user" WHERE owner IN (\'acme\', \'globex\') ORDER BY owner, name') == sorted(SEED_USERS)
    for (owner, name), fields in SEED_USERS.items():
        assert_row(d.row(owner, name), owner, name, **fields)
    listing = ok(d.get("/api/get-users?owner=globex", "globex"))
    assert sorted(u["name"] for u in listing) == ["dana", "gus"]


def test_config_flag_selects_configuration_file(unstarted):
    d = unstarted
    port = free_port()
    d.write_files(TENANT_INIT, config="settings/other.conf", port=port)
    d.start("-config", "settings/other.conf", "-createDatabase", port=port)
    assert database_exists(d)
    user = ok(d.call("GET", "/api/get-user?id=acme/erin", "acme", port=port))
    assert_profile(user, "acme", "erin", **SEED_USERS[("acme", "erin")])


def test_missing_database_without_create_flag_fails(unstarted):
    d = unstarted
    d.write_files(TENANT_INIT)
    result = d.run_once()
    assert result.returncode != 0
    assert not database_exists(d)


@pytest.mark.timeout(150)
def test_restart_preserves_directory_changes(directory):
    d = directory
    dana = d.user("acme", "dana")
    ok(d.post("/api/update-user?id=acme/dana", {**dana, "title": "Changed", "bio": "acme-sentinel"}), "Affected")
    ok(d.post("/api/add-user", {"owner": "acme", "name": "alex", "displayName": "Alex A", "bio": "alex-acme"}), "Affected")
    ok(d.post("/api/add-user", {"owner": "globex", "name": "alex", "displayName": "Alex G", "bio": "alex-globex"}, "globex"), "Affected")
    ok(d.post("/api/delete-user", {"owner": "globex", "name": "gus"}, "globex"), "Affected")
    kept = d.sql('SELECT * FROM "user" WHERE (owner, name) IN ((\'acme\', \'dana\'), (\'acme\', \'alex\'), (\'globex\', \'alex\'), (\'acme\', \'erin\'), (\'globex\', \'dana\')) ORDER BY owner, name')
    d.stop()
    d.start()
    # existing records are left alone by provisioning; the deleted seed record is provisioned again
    assert d.sql('SELECT * FROM "user" WHERE (owner, name) IN ((\'acme\', \'dana\'), (\'acme\', \'alex\'), (\'globex\', \'alex\'), (\'acme\', \'erin\'), (\'globex\', \'dana\')) ORDER BY owner, name') == kept
    assert_row(d.row("globex", "gus"), "globex", "gus", **SEED_USERS[("globex", "gus")])
    assert_profile(d.user("acme", "dana"), "acme", "dana", password=True, **{**SEED_USERS[("acme", "dana")], "title": "Changed", "bio": "acme-sentinel"})
    assert_profile(d.user("acme", "alex"), "acme", "alex", displayName="Alex A", bio="alex-acme")
    assert_profile(d.user("globex", "alex"), "globex", "alex", displayName="Alex G", bio="alex-globex")
    refused(d.get("/api/get-users?owner=acme", "globex"))


@pytest.mark.timeout(150)
def test_credentials_come_from_stored_applications(directory):
    d = directory
    d.stop()
    d.sql("UPDATE application SET client_secret = %s WHERE name = %s", "rotated-secret-5", "app-acme")
    d.start()
    acme = TENANTS["acme"]
    refused(d.get("/api/get-users?owner=acme", "acme"))
    error(d.get("/api/get-users?owner=acme", ("query", acme["client_id"], acme["secret"])), "Incorrect client secret for application: app-acme")
    listing = ok(d.get("/api/get-users?owner=acme", ("basic", acme["client_id"], "rotated-secret-5")))
    assert sorted(u["name"] for u in listing) == ["dana", "erin"]
    assert d.sql("SELECT client_secret FROM application WHERE name = 'app-acme'") == [("rotated-secret-5",)]
    ok(d.get("/api/get-users?owner=globex", "globex"))


@pytest.mark.timeout(150)
def test_new_organization_added_to_init_data_on_restart(directory):
    d = directory
    tenants = {**TENANTS, "initech": {"app": "app-initech", "client_id": "initech-c1", "secret": "initech-s1"}}
    d.stop()
    d.write_files(init_data(orgs=("acme", "globex", "initech"), tenants=tenants), config=None)
    d.start()
    cred = ("basic", "initech-c1", "initech-s1")
    ok(d.get("/api/get-users?owner=initech", cred), [])
    ok(d.post("/api/add-user", {"owner": "initech", "name": "dana", "displayName": "Dana I"}, cred), "Affected")
    assert d.sql('SELECT owner FROM "user" WHERE name = %s ORDER BY owner', "dana") == [("acme",), ("globex",), ("initech",)]
    refused(d.get("/api/get-user?id=acme/dana", cred))
    refused(d.get("/api/get-user?id=initech/dana", "acme"))
