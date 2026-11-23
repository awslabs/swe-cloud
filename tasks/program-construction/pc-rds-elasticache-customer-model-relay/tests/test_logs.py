"""Usage log queries: /api/log/self filters, /api/log/self/stat, /api/log/self/search, and the administrator's /api/log/ views."""

import time

from checks import failed_envelope, number, ok_envelope


def _self_logs(relay, user, query):
    status, reply = relay.api(user, "GET", f"/api/log/self?p=0&{query}")
    return ok_envelope(status, reply)


def _stat(relay, user, query=""):
    status, reply = relay.api(user, "GET", f"/api/log/self/stat?{query}")
    return ok_envelope(status, reply)


def _activity(relay, tag):
    """One customer with a top-up (type 1) and three consumption records over two tokens and two models, in two seconds."""
    admin = relay.user("default", role=10)
    user = relay.user("multi")
    ok_envelope(*relay.api(admin, "POST", "/api/topup", {"user_id": user["id"], "quota": 1000, "remark": "seed"}))
    alpha = relay.create_token(user, name="alpha")
    beta = relay.create_token(user, name="beta")
    assert relay.chat(alpha["key"], f"one {tag}")[0] == 200
    assert relay.chat(beta["key"], f"#usage:10/5# two {tag}", model="gpt-4")[0] == 200
    relay.wait_for(lambda: relay.user_row(user["id"])["request_count"] == 2)
    time.sleep(2.2)
    assert relay.chat(alpha["key"], f"#usage:20/0# three {tag}", model="gpt-4")[0] == 200
    relay.wait_for(lambda: relay.user_row(user["id"])["request_count"] == 3)
    return admin, user


def test_self_logs_filter_by_type(relay, stubs, tag):
    _, user = _activity(relay, tag)
    assert [e["type"] for e in _self_logs(relay, user, "type=1")] == [1]
    assert [e["type"] for e in _self_logs(relay, user, "type=2")] == [2, 2, 2]
    assert sorted(e["type"] for e in _self_logs(relay, user, "type=0")) == [1, 2, 2, 2]


def test_self_logs_filter_by_token_name(relay, stubs, tag):
    _, user = _activity(relay, tag)
    assert [(e["token_name"], e["quota"]) for e in _self_logs(relay, user, "token_name=alpha")] == [("alpha", 300), ("alpha", 63)]
    assert [(e["token_name"], e["quota"]) for e in _self_logs(relay, user, "token_name=beta")] == [("beta", 300)]
    assert _self_logs(relay, user, "token_name=alp") == []


def test_self_logs_filter_by_model_name(relay, stubs, tag):
    _, user = _activity(relay, tag)
    assert [(e["model_name"], e["token_name"]) for e in _self_logs(relay, user, "model_name=gpt-4")] == [("gpt-4", "alpha"), ("gpt-4", "beta")]
    assert [e["model_name"] for e in _self_logs(relay, user, "model_name=gpt-3.5-turbo")] == ["gpt-3.5-turbo"]


def test_self_logs_filter_by_time_window(relay, stubs, tag):
    _, user = _activity(relay, tag)
    entries = _self_logs(relay, user, "type=2")
    latest, earliest = entries[0]["created_at"], entries[-1]["created_at"]
    assert latest > earliest
    assert [e["quota"] for e in _self_logs(relay, user, f"type=2&start_timestamp={latest}")] == [300]
    assert [e["quota"] for e in _self_logs(relay, user, f"type=2&end_timestamp={latest - 1}")] == [300, 63]
    assert len(_self_logs(relay, user, f"type=2&start_timestamp={earliest}&end_timestamp={latest}")) == 3


def test_self_log_stat_sums_quota(relay, stubs, tag):
    _, user = _activity(relay, tag)
    number(_stat(relay, user)["quota"], 663, "all")
    number(_stat(relay, user, "type=2&model_name=gpt-4")["quota"], 600, "gpt-4")
    number(_stat(relay, user, "type=2&token_name=alpha")["quota"], 363, "alpha")
    number(_stat(relay, user, "type=1")["quota"], 663, "consumption whatever the type filter")
    assert set(_stat(relay, user)) == {"quota"}


def test_self_log_search_matches_type_keyword(relay, stubs, tag):
    _, user = _activity(relay, tag)
    status, reply = relay.api(user, "GET", "/api/log/self/search?keyword=2")
    assert sorted(e["quota"] for e in ok_envelope(status, reply)) == [63, 300, 300]
    status, reply = relay.api(user, "GET", "/api/log/self/search?keyword=1")
    assert [e["type"] for e in ok_envelope(status, reply)] == [1]


def test_admin_log_listing_filters_by_username(relay, stubs, tag):
    admin, user = _activity(relay, tag)
    status, reply = relay.api(admin, "GET", f"/api/log/?p=0&type=2&username={user['username']}")
    entries = ok_envelope(status, reply)
    assert [(e["user_id"], e["quota"], e["model_name"]) for e in entries] == [
        (user["id"], 300, "gpt-4"), (user["id"], 300, "gpt-4"), (user["id"], 63, "gpt-3.5-turbo")]
    ids = [e["id"] for e in entries]
    assert all(isinstance(i, int) and i > 0 for i in ids) and ids == sorted(ids, reverse=True), ids
    own = _self_logs(relay, user, "type=2")
    assert [e["id"] for e in own] == [0, 0, 0]


def test_admin_log_stat_for_one_user(relay, stubs, tag):
    admin, user = _activity(relay, tag)
    status, reply = relay.api(admin, "GET", f"/api/log/stat?type=2&username={user['username']}")
    number(ok_envelope(status, reply)["quota"], 663, "quota")
    status, reply = relay.api(admin, "GET", f"/api/log/stat?type=2&username={user['username']}&model_name=gpt-3.5-turbo")
    number(ok_envelope(status, reply)["quota"], 63, "quota")


def test_customers_cannot_read_everyones_logs(relay):
    customer = relay.user("vip")
    for path in ("/api/log/?p=0", "/api/log/stat", "/api/log/search?keyword=1"):
        status, reply = relay.api(customer, "GET", path)
        failed_envelope(reply, status)
