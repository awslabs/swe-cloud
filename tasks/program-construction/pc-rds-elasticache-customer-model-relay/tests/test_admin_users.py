"""Administrator user management: /api/user/ search, read, create, update, manage, delete and /api/group/, with role guards.

Roles: 1 customer, 10 administrator, 100 root. Fixtures insert administrators and roots the same way as customers.
"""

import secrets

from checks import failed_envelope, number, ok_envelope
from conftest import GROUP_RATIO

USER_FIELDS = {"id", "username", "password", "display_name", "role", "status", "email", "github_id", "wechat_id", "lark_id",
               "oidc_id", "verification_code", "access_token", "quota", "used_quota", "request_count", "group", "aff_code",
               "inviter_id"}


def _admin(relay, role=10):
    return relay.user("default", role=role)


def _row(relay, user_id):
    found = relay.rows('SELECT id, username, display_name, role, status, quota, "group", access_token FROM users WHERE id = %s', user_id)
    return found[0] if found else None


def _manage(relay, actor, target, action):
    return relay.api(actor, "POST", "/api/user/manage", {"username": target["username"], "action": action})


def test_search_users_by_username_prefix(relay):
    admin = _admin(relay)
    target = relay.user("svip", quota=4321)
    status, reply = relay.api(admin, "GET", f"/api/user/search?keyword={target['username']}")
    [found] = ok_envelope(status, reply)
    assert set(found) == USER_FIELDS, sorted(found)
    expected = {"id": target["id"], "username": target["username"], "display_name": target["username"], "role": 1, "status": 1,
                "access_token": target["access"], "quota": 4321, "used_quota": 0, "request_count": 0, "group": "svip", "password": ""}
    for field, value in expected.items():
        assert found[field] == value, (field, found[field])


def test_get_user_by_id_respects_roles(relay):
    admin, other_admin, root = _admin(relay), _admin(relay), _admin(relay, role=100)
    customer = relay.user("vip", quota=55)
    status, reply = relay.api(admin, "GET", f"/api/user/{customer['id']}")
    data = ok_envelope(status, reply)
    assert (data["id"], data["username"], data["quota"], data["password"]) == (customer["id"], customer["username"], 55, ""), data
    for target in (other_admin, root):
        status, reply = relay.api(admin, "GET", f"/api/user/{target['id']}")
        failed_envelope(reply, status)
    status, reply = relay.api(root, "GET", f"/api/user/{other_admin['id']}")
    assert ok_envelope(status, reply)["role"] == 10


def test_admin_creates_a_customer(relay):
    admin = _admin(relay)
    name = "n" + secrets.token_hex(4)
    status, reply = relay.api(admin, "POST", "/api/user/", {"username": name, "password": "createdpw1", "display_name": ""})
    ok_envelope(status, reply)
    [row] = relay.rows('SELECT username, display_name, role, status, quota, "group", password FROM users WHERE username = %s', name)
    assert (row["display_name"], row["role"], row["status"], row["quota"], row["group"]) == (name, 1, 1, 0, "default"), row
    assert row["password"] and row["password"] != "createdpw1", "the password is stored hashed"


def test_create_user_validation_and_role_limit(relay):
    admin = _admin(relay)
    stem = "v" + secrets.token_hex(3)
    refused = [
        {"username": stem + "x" * 6, "password": "longenough1"},            # 13 characters
        {"username": stem + "a", "password": "short"},
        {"username": stem + "b", "password": "p" * 21},
        {"username": stem + "c", "password": "longenough1", "display_name": "d" * 21},
        {"username": stem + "d", "password": "longenough1", "role": 10},
    ]
    for body in refused:
        status, reply = relay.api(admin, "POST", "/api/user/", body)
        failed_envelope(reply, status)
    assert relay.rows("SELECT id FROM users WHERE username LIKE %s", stem + "%") == []
    existing = relay.user("vip")
    status, reply = relay.api(admin, "POST", "/api/user/", {"username": existing["username"], "password": "longenough1"})
    failed_envelope(reply, status)
    assert len(relay.rows("SELECT id FROM users WHERE username = %s", existing["username"])) == 1


def test_update_user_fields(relay):
    admin = _admin(relay)
    customer = relay.user("vip", quota=1_000_000)
    status, reply = relay.api(admin, "PUT", "/api/user/", {"id": customer["id"], "username": customer["username"], "display_name": "Renamed",
                                                            "group": "svip", "quota": 777, "password": ""})
    ok_envelope(status, reply)
    row = _row(relay, customer["id"])
    assert (row["display_name"], row["group"], row["quota"], row["access_token"]) == ("Renamed", "svip", 777, customer["access"]), row
    status, reply = relay.api(admin, "GET", f"/api/log/?p=0&type=3&username={customer['username']}")
    [entry] = ok_envelope(status, reply)
    assert (entry["type"], entry["user_id"], entry["username"]) == (3, customer["id"], customer["username"]), entry


def test_update_refused_for_same_or_higher_role(relay):
    admin, other_admin = _admin(relay), _admin(relay)
    before = _row(relay, other_admin["id"])
    status, reply = relay.api(admin, "PUT", "/api/user/", {"id": other_admin["id"], "username": other_admin["username"],
                                                            "display_name": "Hijacked", "quota": 1})
    failed_envelope(reply, status)
    assert _row(relay, other_admin["id"]) == before


def test_disable_and_enable_a_customer(relay):
    admin = _admin(relay)
    customer = relay.user("vip")
    status, reply = _manage(relay, admin, customer, "disable")
    number(ok_envelope(status, reply)["status"], 2, "status")
    assert _row(relay, customer["id"])["status"] == 2
    status, reply = relay.api(customer, "GET", "/api/user/self")
    failed_envelope(reply, status)
    status, reply = _manage(relay, admin, customer, "enable")
    number(ok_envelope(status, reply)["status"], 1, "status")
    assert _row(relay, customer["id"])["status"] == 1
    status, reply = relay.api(customer, "GET", "/api/user/self")
    assert ok_envelope(status, reply)["id"] == customer["id"]


def test_promote_and_demote_need_root(relay):
    admin, root = _admin(relay), _admin(relay, role=100)
    customer = relay.user("vip")
    status, reply = _manage(relay, admin, customer, "promote")
    failed_envelope(reply, status)
    assert _row(relay, customer["id"])["role"] == 1
    status, reply = _manage(relay, root, customer, "promote")
    number(ok_envelope(status, reply)["role"], 10, "role")
    assert _row(relay, customer["id"])["role"] == 10
    status, reply = _manage(relay, root, customer, "promote")
    failed_envelope(reply, status)
    status, reply = _manage(relay, root, customer, "demote")
    number(ok_envelope(status, reply)["role"], 1, "role")
    status, reply = _manage(relay, root, customer, "demote")
    failed_envelope(reply, status)
    assert _row(relay, customer["id"])["role"] == 1


def test_manage_guards_protect_peers_and_root(relay):
    admin, other_admin, root, other_root = _admin(relay), _admin(relay), _admin(relay, role=100), _admin(relay, role=100)
    snapshot = {u["id"]: _row(relay, u["id"]) for u in (other_admin, other_root)}
    for actor, target, action in ((admin, other_admin, "disable"), (admin, other_root, "delete"), (root, other_root, "disable"),
                                  (root, other_root, "delete"), (root, other_root, "demote")):
        status, reply = _manage(relay, actor, target, action)
        failed_envelope(reply, status)
    assert {u: _row(relay, u) for u in snapshot} == snapshot
    status, reply = relay.api(root, "POST", "/api/user/manage", {"username": "nobody" + secrets.token_hex(2), "action": "enable"})
    failed_envelope(reply, status)


def test_manage_delete_retires_the_account(relay):
    admin = _admin(relay)
    customer = relay.user("vip")
    status, reply = _manage(relay, admin, customer, "delete")
    ok_envelope(status, reply)
    row = _row(relay, customer["id"])
    assert row["status"] == 3 and row["username"].startswith("deleted_") and row["username"] != customer["username"], row
    status, reply = relay.api(customer, "GET", "/api/user/self")
    failed_envelope(reply, status)
    status, reply = relay.api(admin, "GET", f"/api/user/{customer['id']}")
    data = ok_envelope(status, reply)
    assert (data["status"], data["username"], data["display_name"]) == (3, row["username"], customer["username"]), data


def test_delete_user_by_id(relay):
    admin = _admin(relay)
    customer, other_admin = relay.user("vip"), _admin(relay)
    status, _ = relay.api(admin, "DELETE", f"/api/user/{customer['id']}")
    assert status == 200
    assert _row(relay, customer["id"])["status"] == 3
    before = _row(relay, other_admin["id"])
    status, reply = relay.api(admin, "DELETE", f"/api/user/{other_admin['id']}")
    failed_envelope(reply, status)
    assert _row(relay, other_admin["id"]) == before


def test_customers_cannot_use_admin_user_routes(relay):
    customer, other = relay.user("vip"), relay.user("vip")
    for method, path, body in (("GET", "/api/user/?p=0", None), ("GET", f"/api/user/{other['id']}", None),
                               ("GET", "/api/user/search?keyword=u", None), ("GET", "/api/group/", None),
                               ("POST", "/api/user/manage", {"username": other["username"], "action": "disable"}),
                               ("PUT", "/api/user/", {"id": other["id"], "username": other["username"], "quota": 1})):
        status, reply = relay.api(customer, method, path, body)
        failed_envelope(reply, status)
    assert relay.user_row(other["id"]) == {"quota": 1_000_000, "used_quota": 0, "request_count": 0, "status": 1}


def test_groups_list_the_configured_group_ratios(relay):
    admin = _admin(relay)
    status, reply = relay.api(admin, "GET", "/api/group/")
    assert sorted(ok_envelope(status, reply)) == sorted(GROUP_RATIO)
