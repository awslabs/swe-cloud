"""Setting user passwords, the organization's password options and password storage types."""

import pytest

from support import TENANTS, error, form_post, ok, refused, wider_init

ACME = TENANTS["acme"]
GLOBEX = TENANTS["globex"]


@pytest.fixture
def pw(launch):
    """acme's application allows the password grant, so a password can be checked by signing in with it."""
    return launch(wider_init(grant_types=["client_credentials", "password"]))


def signs_in(d, username, password, tenant=ACME):
    reply = form_post(d, "/api/login/oauth/access_token", {"grant_type": "password", "client_id": tenant["client_id"],
                                                            "client_secret": tenant["secret"], "username": username, "password": password})
    assert reply.code in (200, 400), reply
    return reply.code == 200


def set_password(d, owner, name, new, cred="acme"):
    return d.call("POST", f"/api/set-password?userOwner={owner}&userName={name}&newPassword={new}", cred)


def stored_password(d, owner, name):
    return d.sql('SELECT password FROM "user" WHERE owner = %s AND name = %s', owner, name)[0][0]


def require(d, *options):
    body = {**ok(d.get("/api/get-organization?id=admin/acme")), "passwordOptions": list(options)}
    ok(d.post("/api/update-organization?id=admin/acme", body), "Affected")


def test_set_password_replaces_the_secret(pw):
    old = stored_password(pw, "acme", "dana")
    ok(set_password(pw, "acme", "dana", "Dana-pass-2"), None)
    assert stored_password(pw, "acme", "dana") not in (old, "", "Dana-pass-2")
    assert signs_in(pw, "dana", "Dana-pass-2")
    assert not signs_in(pw, "dana", "Dana-pass-1")


def test_set_password_gives_a_user_a_first_password(pw):
    ok(set_password(pw, "acme", "erin", "Erin-pass-1"), None)
    assert signs_in(pw, "erin", "Erin-pass-1")
    assert pw.user("acme", "erin")["password"] == "***"


def test_set_password_for_missing_user(pw):
    error(set_password(pw, "acme", "nobody", "Some-pass-1"), "The user: acme/nobody doesn't exist")


def test_set_password_requires_a_new_password(pw):
    before = pw.users_snapshot()
    error(set_password(pw, "acme", "dana", ""), "Password cannot be empty")
    assert pw.users_snapshot() == before


def test_set_password_in_other_organization_is_refused(pw):
    before = pw.users_snapshot()
    refused(set_password(pw, "globex", "gus", "Gus-pass-1"))
    assert pw.users_snapshot() == before


def test_password_options_minimum_length(pw):
    require(pw, "AtLeast8")
    before = pw.users_snapshot()
    error(set_password(pw, "acme", "dana", "Ab1"), "The password must have at least 8 characters")
    assert pw.users_snapshot() == before
    ok(set_password(pw, "acme", "dana", "abcdefgh"), None)


def test_password_options_character_classes(pw):
    require(pw, "Aa123")
    error(set_password(pw, "acme", "dana", "lowercase1"), "The password must contain at least one uppercase letter, one lowercase letter and one digit")
    ok(set_password(pw, "acme", "dana", "Mixed1"), None)
    assert signs_in(pw, "dana", "Mixed1")


def test_update_user_enforces_password_options(pw):
    require(pw, "AtLeast8")
    dana = pw.user("acme", "dana")
    before = pw.users_snapshot()
    error(pw.post("/api/update-user?id=acme/dana", {**dana, "password": "short"}), "The password must have at least 8 characters")
    assert pw.users_snapshot() == before


def test_add_user_does_not_apply_password_options(pw):
    require(pw, "AtLeast8", "Aa123")
    ok(pw.post("/api/add-user", {"owner": "acme", "name": "weak", "password": "abc"}), "Affected")
    assert signs_in(pw, "weak", "abc")


def test_password_added_with_user_signs_in(pw):
    ok(pw.post("/api/add-user", {"owner": "acme", "name": "nina", "password": "Nina-pass-1"}), "Affected")
    assert stored_password(pw, "acme", "nina") not in ("", "Nina-pass-1")
    assert signs_in(pw, "nina", "Nina-pass-1")
    assert not signs_in(pw, "nina", "nina-pass-1")


def test_plain_password_type_stores_clear_text(launch):
    init = wider_init(grant_types=["client_credentials", "password"], org_fields={"passwordType": "plain"})
    d = launch(init)
    ok(d.post("/api/add-user", {"owner": "acme", "name": "pat", "password": "Pat-pass-1"}), "Affected")
    assert d.sql('SELECT password, password_type FROM "user" WHERE owner = %s AND name = %s', "acme", "pat") == [("Pat-pass-1", "plain")]
    ok(set_password(d, "acme", "pat", "Pat-pass-2"), None)
    assert stored_password(d, "acme", "pat") == "Pat-pass-2"
    assert signs_in(d, "pat", "Pat-pass-2")
