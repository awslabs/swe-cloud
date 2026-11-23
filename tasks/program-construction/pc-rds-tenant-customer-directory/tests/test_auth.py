"""How a request's application credential is resolved (Basic, query parameters, failures)."""

from support import TENANTS, assert_profile, error, ok, refused

ACME = TENANTS["acme"]


def names(listing):
    return sorted((u["owner"], u["name"]) for u in listing)


def test_basic_and_query_credentials_are_equivalent(directory):
    via_basic = ok(directory.get("/api/get-users?owner=acme", ("basic", ACME["client_id"], ACME["secret"])))
    via_query = ok(directory.get("/api/get-users?owner=acme", ("query", ACME["client_id"], ACME["secret"])))
    assert names(via_basic) == names(via_query) == [("acme", "dana"), ("acme", "erin")]
    ok(directory.post("/api/add-user", {"owner": "acme", "name": "viaq", "title": "Q"}, ("query", ACME["client_id"], ACME["secret"])), "Affected")
    assert directory.row("acme", "viaq")["title"] == "Q"
    ok(directory.post("/api/delete-user", {"owner": "acme", "name": "viaq"}, ("query", ACME["client_id"], ACME["secret"])), "Affected")
    assert directory.row("acme", "viaq") is None


def test_list_without_valid_credential_refused(directory):
    for cred in (
        None,
        ("basic", ACME["client_id"], "wrong-secret"),
        ("basic", "no-such-client", "x"),
        ("basic", TENANTS["globex"]["client_id"], ACME["secret"]),
    ):
        refused(directory.get("/api/get-users?owner=acme", cred))
    refused(directory.get(f"/api/get-users?owner=acme&clientId={ACME['client_id']}", None))


def test_query_credential_failures_name_the_problem(directory):
    error(directory.get("/api/get-users?owner=acme", ("query", ACME["client_id"], "wrong-secret")), "Incorrect client secret for application: app-acme")
    error(directory.get("/api/get-users?owner=acme", ("query", "no-such-client", "x")), "Application not found for client ID: no-such-client")
    before = directory.users_snapshot()
    error(directory.post("/api/add-user", {"owner": "acme", "name": "viaq"}, ("query", ACME["client_id"], "wrong-secret")), "Incorrect client secret for application: app-acme")
    error(directory.get("/api/get-user?id=acme/dana", ("query", "no-such-client", "x")), "Application not found for client ID: no-such-client")
    assert directory.users_snapshot() == before


def test_anonymous_requests(directory):
    before = directory.users_snapshot()
    resp = directory.get("/api/get-user?id=acme/dana", None)
    error(resp, "Please login first")
    assert b"dana@acme.test" not in resp.raw
    error(directory.get("/api/get-user?id=acme/dana", ("basic", ACME["client_id"], "wrong-secret")), "Please login first")
    ok(directory.get("/api/get-user?id=acme/nobody", None), None)
    ok(directory.get("/api/get-user?id=acme/nobody", ("basic", ACME["client_id"], "wrong-secret")), None)
    refused(directory.post("/api/add-user", {"owner": "acme", "name": "anon"}, None))
    refused(directory.post("/api/add-user", {"owner": "acme", "name": "anon"}, ("basic", ACME["client_id"], "wrong-secret")))
    refused(directory.post("/api/delete-user", {"owner": "acme", "name": "erin"}, None))
    refused(directory.post("/api/update-user?id=acme/erin", {"owner": "acme", "name": "erin", "title": "x"}, None))
    assert directory.users_snapshot() == before


def test_credential_resolves_to_its_own_organization(directory):
    user = ok(directory.get("/api/get-user?id=globex/gus", "globex"))
    assert_profile(user, "globex", "gus", displayName="Gus", tag="ops")
    rows = directory.sql("SELECT name, organization, client_id FROM application WHERE client_id IN (%s, %s) ORDER BY name",
                         ACME["client_id"], TENANTS["globex"]["client_id"])
    assert rows == [("app-acme", "acme", ACME["client_id"]), ("app-globex", "globex", TENANTS["globex"]["client_id"])]
