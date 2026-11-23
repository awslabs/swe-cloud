"""The OAuth 2.0 token endpoint, introspection and revocation with application credentials."""

from datetime import datetime, timezone

import pytest

from support import TENANTS, bare_get, form_post, is_uuid, jwt_part, ok, wider_init

ACME = TENANTS["acme"]
CLIENT = {"client_id": ACME["client_id"], "client_secret": ACME["secret"]}
BASIC = (ACME["client_id"], ACME["secret"])
TOKEN = "/api/login/oauth/access_token"


@pytest.fixture
def oauth(launch):
    """acme's application allows client_credentials, password and refresh_token; globex keeps client_credentials only."""
    return launch(wider_init(grant_types=["client_credentials", "password", "refresh_token"]))


def oauth_error(reply, code, error, description):
    assert reply.code == code, reply
    assert reply.json == {"error": error, "error_description": description}, reply


def granted(reply):
    assert reply.code == 200 and isinstance(reply.json, dict) and "access_token" in reply.json, reply
    return reply.json


def password_grant(d, username="dana", password="Dana-pass-1", **extra):
    return form_post(d, TOKEN, {"grant_type": "password", **CLIENT, "username": username, "password": password, **extra})


def assert_lifetime(claims, seconds=3600):
    now = datetime.now(timezone.utc).timestamp()
    assert claims["exp"] - claims["iat"] == seconds, claims
    assert abs(claims["iat"] - now) < 60, (claims["iat"], now)


def test_client_credentials_reply(oauth):
    reply = granted(form_post(oauth, TOKEN, {"grant_type": "client_credentials", **CLIENT}))
    assert {k: reply[k] for k in ("token_type", "expires_in", "scope")} == {"token_type": "Bearer", "expires_in": 3600, "scope": ""}
    assert "refresh_token" not in reply or reply["refresh_token"] == ""


def test_client_credentials_token_identifies_the_application(oauth):
    token = granted(form_post(oauth, TOKEN, {"grant_type": "client_credentials", **CLIENT}))["access_token"]
    claims = jwt_part(token, 1)
    expected = {"owner": "admin", "name": "app-acme", "id": "admin/app-acme", "type": "application", "tokenType": "access-token",
                "sub": "admin/app-acme", "aud": [ACME["client_id"]], "azp": ACME["client_id"], "iss": f"http://127.0.0.1:{oauth.port}"}
    assert {k: claims.get(k) for k in expected} == expected
    assert_lifetime(claims)
    assert jwt_part(token, 0)["alg"] == "RS256"


def test_client_credentials_accepts_basic_authentication(oauth):
    token = granted(form_post(oauth, TOKEN, {"grant_type": "client_credentials"}, basic=BASIC))["access_token"]
    assert jwt_part(token, 1)["sub"] == "admin/app-acme"


def test_wrong_client_secret_is_an_invalid_client(oauth):
    oauth_error(form_post(oauth, TOKEN, {"grant_type": "client_credentials", "client_id": ACME["client_id"], "client_secret": "wrong"}),
                401, "invalid_client", "client_secret is invalid")


def test_unknown_client_id_is_an_invalid_client(oauth):
    oauth_error(form_post(oauth, TOKEN, {"grant_type": "client_credentials", "client_id": "nope", "client_secret": "x"}),
                401, "invalid_client", "client_id is invalid")


def test_grant_type_not_enabled_for_the_application(oauth):
    oauth_error(form_post(oauth, TOKEN, {"grant_type": "made-up", **CLIENT}), 400, "unsupported_grant_type",
                "grant_type: made-up is not supported in this application")
    globex = {"client_id": TENANTS["globex"]["client_id"], "client_secret": TENANTS["globex"]["secret"]}
    oauth_error(form_post(oauth, TOKEN, {"grant_type": "password", **globex, "username": "gus", "password": "x"}), 400,
                "unsupported_grant_type", "grant_type: password is not supported in this application")


def test_password_grant_issues_user_tokens(oauth):
    reply = granted(password_grant(oauth))
    assert {"access_token", "id_token", "refresh_token", "token_type", "expires_in"} <= set(reply), reply
    claims = jwt_part(reply["access_token"], 1)
    user_id = oauth.row("acme", "dana")["id"]
    assert (claims["owner"], claims["name"], claims["sub"], claims["tokenType"]) == ("acme", "dana", user_id, "access-token")
    assert (claims["email"], claims["displayName"], claims["aud"]) == ("dana@acme.test", "Dana Acme", [ACME["client_id"]])
    assert_lifetime(claims)


def test_password_grant_id_and_refresh_tokens(oauth):
    reply = granted(password_grant(oauth))
    id_claims, refresh_claims = jwt_part(reply["id_token"], 1), jwt_part(reply["refresh_token"], 1)
    assert id_claims["tokenType"] == "id-token" and refresh_claims["tokenType"] == "refresh-token"
    assert is_uuid(id_claims["sub"]) and id_claims["sub"] == refresh_claims["sub"] == oauth.row("acme", "dana")["id"]


def test_wrong_password_reports_remaining_attempts(oauth):
    oauth_error(password_grant(oauth, password="bad"), 400, "invalid_grant",
                "invalid username or password: password or code is incorrect, you have 4 remaining chances")
    oauth_error(password_grant(oauth, password="bad"), 400, "invalid_grant",
                "invalid username or password: password or code is incorrect, you have 3 remaining chances")


def test_five_wrong_passwords_lock_the_user(oauth):
    for _ in range(5):
        password_grant(oauth, password="bad")
    locked = ("invalid username or password: You have entered the wrong password or code too many times, "
              "please wait for 15 minutes and try again")
    oauth_error(password_grant(oauth, password="bad"), 400, "invalid_grant", locked)
    oauth_error(password_grant(oauth), 400, "invalid_grant", locked)


def test_password_grant_only_finds_users_of_the_applications_organization(oauth):
    for username in ("nobody", "gus"):
        oauth_error(password_grant(oauth, username=username, password="x"), 400, "invalid_grant", "the user does not exist")


def test_password_grant_for_user_without_password(oauth):
    oauth_error(password_grant(oauth, username="erin", password=""), 400, "invalid_grant",
                "OAuth users cannot use password grant type, please use authorization code flow")


def test_refresh_token_grant_replaces_the_access_token(oauth):
    first = granted(password_grant(oauth))
    second = granted(form_post(oauth, TOKEN, {"grant_type": "refresh_token", **CLIENT, "refresh_token": first["refresh_token"]}))
    assert jwt_part(second["access_token"], 1)["sub"] == jwt_part(first["access_token"], 1)["sub"]
    assert form_post(oauth, "/api/login/oauth/introspect", {"token": first["access_token"]}, basic=BASIC).json == {"active": False}
    assert form_post(oauth, "/api/login/oauth/introspect", {"token": second["access_token"]}, basic=BASIC).json["active"] is True


def test_unknown_refresh_token_is_rejected(oauth):
    oauth_error(form_post(oauth, TOKEN, {"grant_type": "refresh_token", **CLIENT, "refresh_token": "garbage"}), 400, "invalid_grant",
                "refresh token is invalid or revoked")


def test_introspection_describes_an_active_token(oauth):
    token = granted(password_grant(oauth))["access_token"]
    reply = form_post(oauth, "/api/login/oauth/introspect", {"token": token, "token_type_hint": "access_token"}, basic=BASIC)
    assert reply.code == 200, reply
    expected = {"active": True, "client_id": ACME["client_id"], "username": "dana", "token_type": "Bearer",
                "sub": oauth.row("acme", "dana")["id"], "aud": [ACME["client_id"]], "iss": f"http://127.0.0.1:{oauth.port}"}
    assert {k: reply.json.get(k) for k in expected} == expected
    assert reply.json["exp"] == jwt_part(token, 1)["exp"]


def test_introspection_of_unknown_token_is_inactive(oauth):
    reply = form_post(oauth, "/api/login/oauth/introspect", {"token": "garbage"}, basic=BASIC)
    assert (reply.code, reply.json) == (200, {"active": False})


def test_introspection_requires_client_authentication(oauth):
    token = granted(password_grant(oauth))["access_token"]
    reply = form_post(oauth, "/api/login/oauth/introspect", {"token": token})
    assert (reply.code, reply.json) == (400, {"error": "invalid_request"})


def test_revocation_deactivates_access_and_refresh(oauth):
    tokens = granted(password_grant(oauth))
    reply = form_post(oauth, "/api/login/oauth/revoke", {"token": tokens["access_token"], "token_type_hint": "access_token", **CLIENT})
    assert (reply.code, reply.raw.strip()) == (200, b"")
    assert form_post(oauth, "/api/login/oauth/introspect", {"token": tokens["access_token"]}, basic=BASIC).json == {"active": False}
    oauth_error(form_post(oauth, TOKEN, {"grant_type": "refresh_token", **CLIENT, "refresh_token": tokens["refresh_token"]}), 400,
                "invalid_grant", "refresh token is expired")


def test_revocation_with_wrong_secret_is_rejected(oauth):
    tokens = granted(password_grant(oauth))
    oauth_error(form_post(oauth, "/api/login/oauth/revoke", {"token": tokens["access_token"], "client_id": ACME["client_id"], "client_secret": "x"}),
                401, "invalid_client", "Invalid application or wrong clientSecret")
    assert form_post(oauth, "/api/login/oauth/introspect", {"token": tokens["access_token"]}, basic=BASIC).json["active"] is True


def test_bearer_token_reads_its_users_account(oauth):
    token = granted(password_grant(oauth))["access_token"]
    reply = bare_get(oauth, "/api/get-account", {"Authorization": "Bearer " + token})
    assert reply.code == 200 and reply.json["status"] == "ok", reply
    assert (reply.json["data"]["owner"], reply.json["data"]["name"], reply.json["data"]["email"]) == ("acme", "dana", "dana@acme.test")
    info = bare_get(oauth, "/api/userinfo", {"Authorization": "Bearer " + token})
    assert (info.code, info.json) == (200, {"sub": oauth.row("acme", "dana")["id"], "iss": f"http://127.0.0.1:{oauth.port}", "aud": ACME["client_id"]})


def test_issued_tokens_are_listed_for_the_organization(oauth):
    granted(password_grant(oauth))
    listing = ok(oauth.get("/api/get-tokens?owner=admin&organization=acme"))
    assert [(t["application"], t["organization"], t["user"], t["expiresIn"], t["tokenType"]) for t in listing] == [
        ("app-acme", "acme", "dana", 3600, "Bearer")]
    assert ok(oauth.get("/api/get-tokens?owner=admin&organization=globex", "globex")) == []
