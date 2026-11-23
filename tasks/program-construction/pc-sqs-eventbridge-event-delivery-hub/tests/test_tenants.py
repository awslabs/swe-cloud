"""Tenant records over the REST API, their Valkey layout, and API authentication."""

import base64
import hashlib
import hmac
import json
import time

from hub import assert_quiet, error, millis, model, recent, same, stored_number, uid


def tenant_hash(hub, tenant):
    raw = hub.engines.valkey().hgetall(hub.key(f"tenant:{{{tenant}}}:tenant"))
    return {k.decode(): v.decode() for k, v in raw.items()}


def tenant_keys(hub, tenant):
    return sorted(k.decode() for k in hub.engines.valkey().scan_iter(match=hub.key(f"tenant:{{{tenant}}}:*")))


def test_create_update_and_retrieve(hub):
    tid = "t-" + uid()
    start = time.time()
    error(hub.request("GET", f"/tenants/{tid}"), 404, "tenant not found")

    created = hub.request("PUT", f"/tenants/{tid}", {"metadata": {"plan": "pro", "region": "eu"}})
    assert created.status == 201, created
    model(created.json, {"id": tid, "destinations_count": 0, "topics": [], "metadata": {"plan": "pro", "region": "eu"}},
          times=("created_at", "updated_at"))
    assert millis(created.json["created_at"]) == millis(created.json["updated_at"])
    recent(created.json["created_at"], start)
    record = tenant_hash(hub, tid)
    assert set(record) == {"id", "entity", "created_at", "updated_at", "metadata"}
    assert (record["id"], record["entity"]) == (tid, "tenant")
    assert stored_number(record["created_at"]) == stored_number(record["updated_at"]) == millis(created.json["created_at"])
    same(json.loads(record["metadata"]), {"plan": "pro", "region": "eu"})

    time.sleep(0.01)
    updated = hub.request("PUT", f"/tenants/{tid}", {"metadata": {"plan": "free"}})
    assert updated.status == 200, updated
    model(updated.json, {"id": tid, "destinations_count": 0, "topics": [], "metadata": {"plan": "free"}},
          times=("created_at", "updated_at"))
    assert millis(updated.json["created_at"]) == millis(created.json["created_at"])
    assert millis(updated.json["updated_at"]) > millis(created.json["created_at"])

    fetched = hub.ok("GET", f"/tenants/{tid}")
    model(fetched, {"id": tid, "destinations_count": 0, "topics": [], "metadata": {"plan": "free"}}, times=("created_at", "updated_at"))
    assert millis(fetched["created_at"]) == millis(created.json["created_at"])
    assert millis(fetched["updated_at"]) == millis(updated.json["updated_at"])
    record = tenant_hash(hub, tid)
    assert stored_number(record["created_at"]) == millis(created.json["created_at"])
    assert stored_number(record["updated_at"]) == millis(updated.json["updated_at"])
    same(json.loads(record["metadata"]), {"plan": "free"})
    assert tenant_keys(hub, tid) == [hub.key(f"tenant:{{{tid}}}:tenant")]


def test_put_without_metadata_clears_it(hub):
    tid = hub.tenant({"a": "1"})
    cleared = hub.request("PUT", f"/tenants/{tid}")
    assert cleared.status == 200, cleared
    model(cleared.json, {"id": tid, "destinations_count": 0, "topics": []}, times=("created_at", "updated_at"))
    assert "metadata" not in tenant_hash(hub, tid)
    model(hub.ok("GET", f"/tenants/{tid}"), {"id": tid, "destinations_count": 0, "topics": []}, times=("created_at", "updated_at"))


def test_tenant_validation_preserves_record(hub):
    tid = hub.tenant({"keep": "me"})
    before = tenant_hash(hub, tid)
    error(hub.request("PUT", f"/tenants/{tid}", raw=b"{"), 422, "invalid JSON")
    error(hub.request("PUT", f"/tenants/{tid}", {"metadata": "text"}), 422, "invalid JSON")
    assert tenant_hash(hub, tid) == before
    coerced = hub.ok("PUT", f"/tenants/{tid}", {"metadata": {"n": 1, "f": 2.5}})
    same(coerced["metadata"], {"n": "1", "f": "2.5"})
    fresh = "t-" + uid()
    error(hub.request("PUT", f"/tenants/{fresh}", raw=b"{"), 422, "invalid JSON")
    error(hub.request("GET", f"/tenants/{fresh}"), 404, "tenant not found")
    assert tenant_keys(hub, fresh) == []


def test_delete_soft_deletes_and_allows_recreation(hub, queues):
    tid = hub.tenant({"x": "y"})
    url = queues.create()
    dest = hub.sqs_destination(tid, url, topics="*", id="d1")
    other = hub.tenant()
    hub.sqs_destination(other, queues.create(), id="d1")
    other_before = tenant_hash(hub, other)

    deleted = hub.request("DELETE", f"/tenants/{tid}")
    assert deleted.status == 200
    same(deleted.json, {"success": True})
    error(hub.request("GET", f"/tenants/{tid}"), 404, "tenant not found")
    error(hub.request("GET", f"/tenants/{tid}/destinations"), 404, "tenant not found")
    error(hub.request("GET", f"/tenants/{tid}/destinations/{dest['id']}"), 404, "tenant not found")
    error(hub.request("DELETE", f"/tenants/{tid}"), 404, "tenant not found")
    same(hub.publish({"tenant_id": tid, "topic": "user.created", "data": {}})["destination_ids"], [])
    assert_quiet(url)

    kv = hub.engines.valkey()
    assert tenant_keys(hub, tid) == sorted([hub.key(f"tenant:{{{tid}}}:tenant"), hub.key(f"tenant:{{{tid}}}:destination:d1")])
    for key in tenant_keys(hub, tid):
        record = {k.decode(): v for k, v in kv.hgetall(key).items()}
        assert abs(float(stored_number(record["deleted_at"])) / 1000 - time.time()) < 120
        assert 0 < kv.ttl(key) <= 7 * 24 * 3600
        assert kv.ttl(key) > 7 * 24 * 3600 - 60
    # The other tenant, with a destination of the same ID, is untouched.
    assert tenant_hash(hub, other) == other_before
    assert [d["id"] for d in hub.ok("GET", f"/tenants/{other}/destinations")] == ["d1"]

    recreated = hub.request("PUT", f"/tenants/{tid}", {})
    assert recreated.status == 201, recreated
    model(recreated.json, {"id": tid, "destinations_count": 0, "topics": []}, times=("created_at", "updated_at"))
    assert hub.ok("GET", f"/tenants/{tid}/destinations") == []
    assert "deleted_at" not in tenant_hash(hub, tid)


def test_authentication(hub):
    tid = hub.tenant()
    for auth in (None, "Bearer wrong", "Basic " + base64.b64encode(f"x:{hub.api_key}".encode()).decode(), hub.api_key):
        error(hub.request("GET", f"/tenants/{tid}", auth=auth), 401, "unauthorized")
        error(hub.request("PUT", f"/tenants/t-{uid()}", {}, auth=auth), 401, "unauthorized")
        error(hub.request("POST", "/publish", {"tenant_id": tid, "topic": "user.created", "data": {}}, auth=auth), 401, "unauthorized")
    error(hub.request("GET", "/nope"), 404, "not found")
    error(hub.request("GET", "/tenants"), 501, "list tenant feature is not enabled")


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_tenant_token_scopes_access(hub, queues):
    tid = hub.tenant()
    other = hub.tenant()
    start = time.time()
    issued = hub.ok("GET", f"/tenants/{tid}/token")
    assert set(issued) == {"tenant_id", "token"} and issued["tenant_id"] == tid
    header, payload, signature = issued["token"].split(".")
    same(json.loads(_unb64(header)), {"alg": "HS256", "typ": "JWT"})
    claims = json.loads(_unb64(payload))
    assert set(claims) == {"sub", "iss", "iat", "exp", "deployment_id"}
    assert claims["sub"] == tid and claims["iss"] == "outpost" and claims["deployment_id"] == hub.deployment
    assert abs(claims["iat"] - start) < 10 and claims["exp"] - claims["iat"] == 24 * 3600
    expected_sig = hmac.new(hub.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert _unb64(signature) == expected_sig

    jwt = "Bearer " + issued["token"]
    model(hub.ok("GET", f"/tenants/{tid}", auth=jwt), {"id": tid, "destinations_count": 0, "topics": []},
          times=("created_at", "updated_at"))
    created = hub.request("POST", f"/tenants/{tid}/destinations",
                          {"type": "aws_sqs", "topics": "*", "config": {"queue_url": queues.create()}, "credentials": {"key": "k", "secret": "s"}},
                          auth=jwt)
    assert created.status == 201, created
    assert [d["id"] for d in hub.ok("GET", f"/tenants/{tid}/destinations", auth=jwt)] == [created.json["id"]]

    error(hub.request("GET", f"/tenants/{tid}/token", auth=jwt), 403, "forbidden")
    error(hub.request("GET", f"/tenants/{other}", auth=jwt), 403, "forbidden")
    error(hub.request("GET", f"/tenants/{other}/destinations", auth=jwt), 403, "forbidden")
    error(hub.request("PUT", f"/tenants/t-{uid()}", {}, auth=jwt), 403, "forbidden")
    error(hub.request("POST", "/publish", {"tenant_id": tid, "topic": "user.created", "data": {}}, auth=jwt), 403, "forbidden")
    assert hub.ok("GET", f"/tenants/{other}/destinations") == []

    forged_sig = _b64(hmac.new(b"another-secret", f"{header}.{payload}".encode(), hashlib.sha256).digest())
    error(hub.request("GET", f"/tenants/{tid}", auth=f"Bearer {header}.{payload}.{forged_sig}"), 401, "unauthorized")
    forged = _b64(json.dumps({**claims, "sub": other}).encode())
    resigned = _b64(hmac.new(hub.jwt_secret.encode(), f"{header}.{forged}".encode(), hashlib.sha256).digest())
    model(hub.ok("GET", f"/tenants/{other}", auth=f"Bearer {header}.{forged}.{resigned}"),
          {"id": other, "destinations_count": 0, "topics": []}, times=("created_at", "updated_at"))
    expired = _b64(json.dumps({**claims, "exp": int(time.time()) - 60}).encode())
    expired_sig = _b64(hmac.new(hub.jwt_secret.encode(), f"{header}.{expired}".encode(), hashlib.sha256).digest())
    error(hub.request("GET", f"/tenants/{tid}", auth=f"Bearer {header}.{expired}.{expired_sig}"), 401, "unauthorized")


def test_token_for_missing_tenant(hub):
    error(hub.request("GET", f"/tenants/t-{uid()}/token"), 404, "tenant not found")
