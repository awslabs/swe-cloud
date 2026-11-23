"""Cross-tenant denial, internal bucket protection and request authentication."""

import os

import pytest
from conftest import ACL_PREFIX, bucket_contents, bucket_names, error_of


@pytest.fixture
def two_buckets(tenants):
    a, b = tenants
    gw = a.gateway
    ba, bb = gw.name("alpha"), gw.name("bravo")
    a.s3.create_bucket(Bucket=ba)
    b.s3.create_bucket(Bucket=bb)
    a.s3.put_object(Bucket=ba, Key="shared/key.txt", Body=b"alpha-bytes\n")
    b.s3.put_object(Bucket=bb, Key="shared/key.txt", Body=b"bravo-bytes\n")
    b.s3.put_object(Bucket=bb, Key="bravo-only.bin", Body=bytes(range(64)))
    return a, b, ba, bb


def snapshot(gw, *buckets):
    return {name: bucket_contents(name) for name in (*buckets, gw.meta, gw.iamb)}


CROSS = {
    "get": (lambda s3, bucket: s3.get_object(Bucket=bucket, Key="shared/key.txt"), "AccessDenied"),
    "put-existing": (lambda s3, bucket: s3.put_object(Bucket=bucket, Key="shared/key.txt", Body=b"evil"), "AccessDenied"),
    "put-new": (lambda s3, bucket: s3.put_object(Bucket=bucket, Key="planted.txt", Body=b"evil"), "AccessDenied"),
    "delete-existing": (lambda s3, bucket: s3.delete_object(Bucket=bucket, Key="shared/key.txt"), "AccessDenied"),
    "delete-missing": (lambda s3, bucket: s3.delete_object(Bucket=bucket, Key="no-such-key"), "AccessDenied"),
    "list-v2": (lambda s3, bucket: s3.list_objects_v2(Bucket=bucket), "AccessDenied"),
    "list-v1": (lambda s3, bucket: s3.list_objects(Bucket=bucket), "AccessDenied"),
    "head-existing": (lambda s3, bucket: s3.head_object(Bucket=bucket, Key="shared/key.txt"), None),
    "head-missing": (lambda s3, bucket: s3.head_object(Bucket=bucket, Key="no-such-key"), None),
    "head-bucket": (lambda s3, bucket: s3.head_bucket(Bucket=bucket), None),
}


@pytest.mark.parametrize("operation", sorted(CROSS))
def test_cross_tenant_request_denied(two_buckets, operation):
    a, b, ba, bb = two_buckets
    gw = a.gateway
    call, code = CROSS[operation]
    before = snapshot(gw, ba, bb)
    for client, target in ((a.s3, bb), (b.s3, ba)):
        status, observed = error_of(lambda: call(client, target))
        assert status == 403, (operation, target)
        if code is not None:
            assert observed == code, (operation, target)
        assert snapshot(gw, ba, bb) == before, (operation, target)
    # Owners are unaffected.
    assert a.s3.get_object(Bucket=ba, Key="shared/key.txt")["Body"].read() == b"alpha-bytes\n"
    assert b.s3.get_object(Bucket=bb, Key="shared/key.txt")["Body"].read() == b"bravo-bytes\n"


def test_same_key_in_two_tenants(two_buckets):
    a, b, ba, bb = two_buckets
    assert bucket_contents(ba) == {"shared/key.txt": b"alpha-bytes\n"}
    assert bucket_contents(bb) == {"shared/key.txt": b"bravo-bytes\n", "bravo-only.bin": bytes(range(64))}
    assert a.s3.get_object(Bucket=ba, Key="shared/key.txt")["Body"].read() == b"alpha-bytes\n"
    assert b.s3.get_object(Bucket=bb, Key="shared/key.txt")["Body"].read() == b"bravo-bytes\n"
    assert [o["Key"] for o in a.s3.list_objects_v2(Bucket=ba)["Contents"]] == ["shared/key.txt"]
    assert [o["Key"] for o in b.s3.list_objects_v2(Bucket=bb)["Contents"]] == ["bravo-only.bin", "shared/key.txt"]
    assert bucket_names(a.s3) == [ba]
    assert bucket_names(b.s3) == [bb]
    gw = a.gateway
    assert gw.acl_record(ba)["Owner"] == a.access
    assert gw.acl_record(bb)["Owner"] == b.access


def test_internal_buckets_are_not_reachable(two_buckets):
    a, b, ba, bb = two_buckets
    gw = a.gateway
    before = snapshot(gw, ba, bb)
    attempts = {
        "list meta": lambda: a.s3.list_objects_v2(Bucket=gw.meta),
        "get acl record": lambda: a.s3.get_object(Bucket=gw.meta, Key=ACL_PREFIX + bb),
        "overwrite acl record": lambda: a.s3.put_object(
            Bucket=gw.meta, Key=ACL_PREFIX + bb, Body=b'{"Owner":"%s"}' % a.access.encode()),
        "delete acl record": lambda: b.s3.delete_object(Bucket=gw.meta, Key=ACL_PREFIX + ba),
        "list accounts": lambda: b.s3.list_objects_v2(Bucket=gw.iamb),
        "get accounts": lambda: a.s3.get_object(Bucket=gw.iamb, Key="users.json"),
        "overwrite accounts": lambda: b.s3.put_object(Bucket=gw.iamb, Key="users.json", Body=b"{}"),
    }
    for label, call in attempts.items():
        assert error_of(call) == (403, "AccessDenied"), label
        assert snapshot(gw, ba, bb) == before, label
    for client in (a.s3, b.s3):
        assert error_of(lambda: client.head_bucket(Bucket=gw.meta))[0] == 403
        assert error_of(lambda: client.head_bucket(Bucket=gw.iamb))[0] == 403
    # Ownership is still enforced from the untouched records.
    assert error_of(lambda: a.s3.get_object(Bucket=bb, Key="shared/key.txt")) == (403, "AccessDenied")


def test_wrong_secret_is_rejected(two_buckets):
    a, b, ba, bb = two_buckets
    gw = a.gateway
    forged = gw.client(a.access, b.secret)
    before = snapshot(gw, ba, bb)
    assert error_of(forged.list_buckets) == (403, "SignatureDoesNotMatch")
    assert error_of(lambda: forged.get_object(Bucket=ba, Key="shared/key.txt")) == (403, "SignatureDoesNotMatch")
    assert error_of(lambda: forged.get_object(Bucket=bb, Key="shared/key.txt")) == (403, "SignatureDoesNotMatch")
    assert error_of(lambda: forged.put_object(Bucket=ba, Key="shared/key.txt", Body=b"forged")) == (403, "SignatureDoesNotMatch")
    assert error_of(lambda: forged.delete_object(Bucket=ba, Key="shared/key.txt")) == (403, "SignatureDoesNotMatch")
    assert error_of(lambda: forged.create_bucket(Bucket=gw.name("forged"))) == (403, "SignatureDoesNotMatch")
    assert snapshot(gw, ba, bb) == before


def test_unknown_access_key_is_rejected(two_buckets):
    a, _, ba, bb = two_buckets
    gw = a.gateway
    stranger = gw.client(f"stranger-{gw.suffix}", a.secret)
    before = snapshot(gw, ba, bb)
    assert error_of(stranger.list_buckets) == (403, "InvalidAccessKeyId")
    assert error_of(lambda: stranger.put_object(Bucket=ba, Key="x", Body=b"x")) == (403, "InvalidAccessKeyId")
    assert snapshot(gw, ba, bb) == before


def test_backend_credentials_are_not_a_tenant(two_buckets):
    a, _, ba, bb = two_buckets
    gw = a.gateway
    upstream_key = gw.client(os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])
    before = snapshot(gw, ba, bb)
    assert error_of(upstream_key.list_buckets) == (403, "InvalidAccessKeyId")
    assert error_of(lambda: upstream_key.get_object(Bucket=ba, Key="shared/key.txt")) == (403, "InvalidAccessKeyId")
    assert snapshot(gw, ba, bb) == before


def test_unsigned_requests_are_denied(two_buckets):
    a, _, ba, bb = two_buckets
    gw = a.gateway
    anon = gw.anonymous()
    before = snapshot(gw, ba, bb)
    assert error_of(anon.list_buckets) == (403, "AccessDenied")
    assert error_of(lambda: anon.get_object(Bucket=ba, Key="shared/key.txt")) == (403, "AccessDenied")
    assert error_of(lambda: anon.put_object(Bucket=bb, Key="anon.txt", Body=b"anon")) == (403, "AccessDenied")
    assert error_of(lambda: anon.list_objects_v2(Bucket=ba)) == (403, "AccessDenied")
    assert snapshot(gw, ba, bb) == before


def test_signature_for_wrong_region_is_rejected(two_buckets):
    a, *_ = two_buckets
    wrong = a.gateway.client(a.access, a.secret, region="us-west-2")
    assert error_of(wrong.list_buckets) == (400, "AuthorizationHeaderMalformed")
