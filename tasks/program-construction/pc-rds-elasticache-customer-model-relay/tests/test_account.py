"""Customer self-service: registration, password login sessions, logout, profile changes, access tokens and account deletion.

Registration and login share a per-client limit of a few dozen calls per run; these tests use about fifteen.
"""

import http.cookiejar
import json
import secrets
import urllib.error
import urllib.request

from checks import failed_envelope, ok_envelope


class Session:
    """A cookie-keeping client, as a browser would be."""

    def __init__(self, relay):
        self.base = relay.base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                return error.code, json.loads(payload)
            except ValueError:
                return error.code, payload.decode(errors="replace")


def _register(relay, password="secret-pass1", **extra):
    name = "r" + secrets.token_hex(4)
    status, reply = relay.call("POST", "/api/user/register", {"username": name, "password": password, **extra})
    ok_envelope(status, reply)
    return name, password


def _row(relay, name):
    found = relay.rows('SELECT id, username, display_name, role, status, quota, "group", access_token, aff_code, inviter_id, password '
                       "FROM users WHERE username = %s", name)
    return found[0] if found else None


def _login(relay, name, password):
    session = Session(relay)
    status, reply = session.call("POST", "/api/user/login", {"username": name, "password": password})
    return session, status, reply


def test_register_creates_a_customer_with_defaults(relay):
    name, password = _register(relay, display_name="Ignored Name")
    row = _row(relay, name)
    assert (row["display_name"], row["role"], row["status"], row["quota"], row["group"], row["inviter_id"]) == (name, 1, 1, 0, "default", 0), row
    assert len(row["access_token"]) == 32 and len(row["aff_code"]) == 4 and row["aff_code"].isalnum(), row
    assert row["password"] and row["password"] != password


def test_register_validation(relay):
    taken, _ = _register(relay)
    stem = "q" + secrets.token_hex(3)
    for body in ({"username": stem + "a", "password": "short"}, {"username": stem + "b" * 7, "password": "secret-pass1"},
                 {"username": taken, "password": "secret-pass1"}):
        status, reply = relay.call("POST", "/api/user/register", body)
        failed_envelope(reply, status)
    assert relay.rows("SELECT id FROM users WHERE username LIKE %s", stem + "%") == []
    assert len(relay.rows("SELECT id FROM users WHERE username = %s", taken)) == 1


def test_register_with_an_affiliate_code_records_the_inviter(relay):
    inviter = relay.user("vip")
    code = relay.rows("SELECT aff_code FROM users WHERE id = %s", inviter["id"])[0]["aff_code"]
    name, _ = _register(relay, aff_code=code)
    assert _row(relay, name)["inviter_id"] == inviter["id"]


def test_login_session_authenticates_the_management_api(relay):
    name, password = _register(relay)
    row = _row(relay, name)
    session, status, reply = _login(relay, name, password)
    data = ok_envelope(status, reply)
    expected = {"id": row["id"], "username": name, "display_name": name, "role": 1, "status": 1, "password": "", "access_token": "",
                "quota": 0, "group": ""}
    for field, value in expected.items():
        assert data[field] == value, (field, data[field])
    status, reply = session.call("GET", "/api/user/self")
    me = ok_envelope(status, reply)
    assert (me["id"], me["username"], me["access_token"], me["group"]) == (row["id"], name, row["access_token"], "default"), me
    status, reply = session.call("POST", "/api/token/", {"name": "from-session", "expired_time": -1})
    token = ok_envelope(status, reply)
    assert token["user_id"] == row["id"] and token["name"] == "from-session"


def test_login_refuses_wrong_password_and_disabled_account(relay):
    name, password = _register(relay)
    session, status, reply = _login(relay, name, "not-the-password")
    failed_envelope(reply, status)
    assert session.call("GET", "/api/user/self")[0] == 401
    relay.q("UPDATE users SET status = 2 WHERE username = %s", name)
    session, status, reply = _login(relay, name, password)
    failed_envelope(reply, status)


def test_logout_ends_the_session(relay):
    name, password = _register(relay)
    session, status, reply = _login(relay, name, password)
    ok_envelope(status, reply)
    status, reply = session.call("GET", "/api/user/logout")
    ok_envelope(status, reply)
    status, reply = session.call("GET", "/api/user/self")
    failed_envelope(reply, status, http_status=401)


def test_update_self_changes_display_name_and_password(relay):
    name, password = _register(relay)
    access = _row(relay, name)["access_token"]
    status, reply = relay.call("PUT", "/api/user/self", {"display_name": "New Name", "password": "changed-pass2"}, auth=access)
    ok_envelope(status, reply)
    row = _row(relay, name)
    assert (row["display_name"], row["username"], row["role"], row["quota"]) == ("New Name", name, 1, 0), row
    _, status, reply = _login(relay, name, password)
    failed_envelope(reply, status)
    _, status, reply = _login(relay, name, "changed-pass2")
    assert ok_envelope(status, reply)["display_name"] == "New Name"


def test_update_self_validation(relay):
    customer = relay.user("vip")
    before = relay.rows("SELECT display_name, password FROM users WHERE id = %s", customer["id"])[0]
    for body in ({"display_name": "d" * 21}, {"display_name": "ok", "password": "short"}):
        status, reply = relay.api(customer, "PUT", "/api/user/self", body)
        failed_envelope(reply, status)
    assert relay.rows("SELECT display_name, password FROM users WHERE id = %s", customer["id"])[0] == before


def test_regenerating_the_access_token_retires_the_old_one(relay):
    customer = relay.user("vip")
    status, reply = relay.api(customer, "GET", "/api/user/token")
    fresh = ok_envelope(status, reply)
    assert isinstance(fresh, str) and len(fresh) == 32 and fresh != customer["access"], fresh
    assert relay.rows("SELECT access_token FROM users WHERE id = %s", customer["id"])[0]["access_token"] == fresh
    status, reply = relay.api(customer, "GET", "/api/user/self")
    failed_envelope(reply, status)
    status, reply = relay.call("GET", "/api/user/self", auth=fresh)
    assert ok_envelope(status, reply)["id"] == customer["id"]


def test_aff_code_returns_the_stored_code(relay):
    customer = relay.user("vip")
    code = relay.rows("SELECT aff_code FROM users WHERE id = %s", customer["id"])[0]["aff_code"]
    for _ in range(2):
        status, reply = relay.api(customer, "GET", "/api/user/aff")
        assert ok_envelope(status, reply) == code


def test_delete_self_retires_the_account(relay):
    customer = relay.user("vip")
    token = relay.create_token(customer, name="left-behind")
    status, reply = relay.api(customer, "DELETE", "/api/user/self")
    ok_envelope(status, reply)
    row = relay.rows("SELECT username, status FROM users WHERE id = %s", customer["id"])[0]
    assert row["status"] == 3 and row["username"].startswith("deleted_"), row
    status, reply = relay.api(customer, "GET", "/api/token/?p=0")
    failed_envelope(reply, status)
    assert relay.token_row(token["id"]) is not None


def test_root_cannot_delete_itself(relay):
    root = relay.user("default", role=100)
    status, reply = relay.api(root, "DELETE", "/api/user/self")
    failed_envelope(reply, status)
    assert relay.rows("SELECT status FROM users WHERE id = %s", root["id"])[0]["status"] == 1
