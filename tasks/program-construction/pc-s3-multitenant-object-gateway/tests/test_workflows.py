"""Multi-step sequences: interleaved tenants, restarts and accounts added later."""

from conftest import bucket_contents, bucket_names, error_of


def test_interleaved_valid_and_forged_requests(tenants):
    a, b = tenants
    gw = a.gateway
    ba, bb = gw.name("ia"), gw.name("ib")
    a.s3.create_bucket(Bucket=ba)
    b.s3.create_bucket(Bucket=bb)
    a.s3.put_object(Bucket=ba, Key="obj", Body=b"from a")
    b.s3.put_object(Bucket=bb, Key="obj", Body=b"from b")
    a_with_b_secret = gw.client(a.access, b.secret)
    b_with_a_secret = gw.client(b.access, a.secret)

    for _ in range(2):
        assert a.s3.get_object(Bucket=ba, Key="obj")["Body"].read() == b"from a"
        assert error_of(lambda: a_with_b_secret.get_object(Bucket=bb, Key="obj")) == (403, "SignatureDoesNotMatch")
        assert b.s3.get_object(Bucket=bb, Key="obj")["Body"].read() == b"from b"
        assert error_of(lambda: b_with_a_secret.put_object(Bucket=ba, Key="obj", Body=b"hijack")) == (403, "SignatureDoesNotMatch")
        # A valid request right after a forged one is still judged on its own identity.
        assert error_of(lambda: b.s3.get_object(Bucket=ba, Key="obj")) == (403, "AccessDenied")
        assert error_of(lambda: a.s3.put_object(Bucket=bb, Key="obj", Body=b"hijack")) == (403, "AccessDenied")
        assert bucket_contents(ba) == {"obj": b"from a"}
        assert bucket_contents(bb) == {"obj": b"from b"}
    assert bucket_names(a.s3) == [ba]
    assert bucket_names(b.s3) == [bb]


def test_two_tenant_lifecycle(tenants):
    a, b = tenants
    gw = a.gateway
    ba, bb = gw.name("life-a"), gw.name("life-b")
    a.s3.create_bucket(Bucket=ba)
    b.s3.create_bucket(Bucket=bb)
    for key in ("one", "two", "three"):
        a.s3.put_object(Bucket=ba, Key=key, Body=b"a:" + key.encode())
        b.s3.put_object(Bucket=bb, Key=key, Body=b"b:" + key.encode())
    a.s3.delete_object(Bucket=ba, Key="two")
    assert bucket_contents(ba) == {"one": b"a:one", "three": b"a:three"}
    assert bucket_contents(bb) == {"one": b"b:one", "two": b"b:two", "three": b"b:three"}
    assert sorted(o["Key"] for o in a.s3.list_objects_v2(Bucket=ba)["Contents"]) == ["one", "three"]
    assert sorted(o["Key"] for o in b.s3.list_objects_v2(Bucket=bb)["Contents"]) == ["one", "three", "two"]
    b.s3.put_object(Bucket=bb, Key="one", Body=b"b:one-v2")
    assert bucket_contents(ba)["one"] == b"a:one"
    assert a.s3.get_object(Bucket=ba, Key="one")["Body"].read() == b"a:one"
    assert b.s3.get_object(Bucket=bb, Key="one")["Body"].read() == b"b:one-v2"
    # A second bucket for B shows up only in B's listing.
    bb2 = gw.name("life-b2")
    b.s3.create_bucket(Bucket=bb2)
    assert bucket_names(a.s3) == [ba]
    assert bucket_names(b.s3) == sorted([bb, bb2])
    assert gw.acl_record(bb2)["Owner"] == b.access


def test_restart_preserves_accounts_and_ownership(tenants):
    a, b = tenants
    gw = a.gateway
    ba, bb = gw.name("keep-a"), gw.name("keep-b")
    a.s3.create_bucket(Bucket=ba)
    b.s3.create_bucket(Bucket=bb)
    a.s3.put_object(Bucket=ba, Key="persisted", Body=b"survives restart")
    users_before = gw.users()
    meta_before = bucket_contents(gw.meta)

    gw.stop()
    assert gw.users() == users_before
    assert bucket_contents(gw.meta) == meta_before
    gw.start()

    a2, b2 = gw.client(a.access, a.secret), gw.client(b.access, b.secret)
    assert bucket_names(a2) == [ba]
    assert bucket_names(b2) == [bb]
    assert a2.get_object(Bucket=ba, Key="persisted")["Body"].read() == b"survives restart"
    assert error_of(lambda: b2.get_object(Bucket=ba, Key="persisted")) == (403, "AccessDenied")
    assert error_of(lambda: b2.create_bucket(Bucket=ba)) == (409, "BucketAlreadyExists")
    assert error_of(lambda: a2.create_bucket(Bucket=ba)) == (409, "BucketAlreadyOwnedByYou")
    assert error_of(lambda: gw.client(a.access, b.secret).list_buckets()) == (403, "SignatureDoesNotMatch")

    c = gw.create_user(f"tenantc-{gw.suffix}", "secret-charlie-3")
    assert set(gw.users()["accessAccounts"]) == {a.access, b.access, c.access}
    assert bucket_names(c.s3) == []


def test_account_added_after_traffic_is_isolated(tenants):
    a, b = tenants
    gw = a.gateway
    ba = gw.name("early")
    a.s3.create_bucket(Bucket=ba)
    a.s3.put_object(Bucket=ba, Key="k", Body=b"early bytes")
    assert bucket_names(b.s3) == []

    c = gw.create_user(f"latecomer-{gw.suffix}", "secret-late")
    assert bucket_names(c.s3) == []
    assert error_of(lambda: c.s3.get_object(Bucket=ba, Key="k")) == (403, "AccessDenied")
    assert error_of(lambda: c.s3.create_bucket(Bucket=ba)) == (409, "BucketAlreadyExists")
    bc = gw.name("late")
    c.s3.create_bucket(Bucket=bc)
    c.s3.put_object(Bucket=bc, Key="k", Body=b"late bytes")
    assert bucket_names(c.s3) == [bc]
    assert bucket_names(a.s3) == [ba]
    assert bucket_names(b.s3) == []
    assert error_of(lambda: a.s3.get_object(Bucket=bc, Key="k")) == (403, "AccessDenied")
    assert bucket_contents(ba) == {"k": b"early bytes"}
    assert bucket_contents(bc) == {"k": b"late bytes"}
    assert gw.acl_record(bc)["Owner"] == c.access
    assert set(bucket_contents(gw.iamb)) == {"users.json"}
