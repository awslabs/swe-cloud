"""Cache state over time: TTL, entry keys, revalidation on expiry, eviction order and label moves."""

import time

import pytest

from conftest import assert_error, assert_matches, assert_same_document, document, free_port, nested_config, service_error

TTL = 2
EXPIRE = TTL + 0.8


@pytest.fixture
def short(launch, recorder):
    """A program with a 2 s TTL whose AWS traffic is recorded."""
    port = free_port()
    program = launch(nested_config(port, f"[capabilities.secrets_manager.cache]\nttl_seconds = {TTL}\n"), endpoint=recorder.url, port=port)
    recorder.take()
    return program


def test_stale_within_ttl_until_refresh_now(agent, recorder, secrets, sm):
    # Adapted from integration-tests/tests/cache_behavior.rs::test_refresh_now_on_updated_secret_succeeds.
    created = secrets.create("stale", SecretString="v1")
    name = created["Name"]
    first = document(agent.query(name))
    assert_matches(first, sm.get_secret_value(SecretId=name))
    v2 = sm.put_secret_value(SecretId=name, SecretString="v2")["VersionId"]
    recorder.take()

    assert_same_document(document(agent.query(name)), first)
    assert_same_document(document(agent.query(name, refreshNow="false")), first)
    assert_same_document(document(agent.query(name, refreshNow="0")), first)
    assert recorder.take() == []

    fresh = document(agent.query(name, refreshNow="TRUE"))
    assert fresh["VersionId"] == v2 and fresh["SecretString"] == "v2"
    assert_matches(fresh, sm.get_secret_value(SecretId=name))
    assert recorder.take() == ["GetSecretValue"]
    # The refreshed value replaced the cache entry.
    assert_same_document(document(agent.query(name)), fresh)
    assert_same_document(document(agent.get("/v1/" + name)), fresh)
    assert recorder.take() == []

    sm.put_secret_value(SecretId=name, SecretString="v3")
    third = document(agent.get(f"/v1/{name}?refreshNow=1"))
    assert third["SecretString"] == "v3"
    assert recorder.take() == ["GetSecretValue"]
    assert document(agent.query(name))["SecretString"] == "v3"
    assert recorder.take() == []


def test_cache_entry_keys(agent, recorder, secrets, sm):
    created = secrets.create("keys", SecretString="v1")
    name, arn, v1 = created["Name"], created["ARN"], created["VersionId"]
    document(agent.query(name))
    assert recorder.take() == ["GetSecretValue"]
    sm.put_secret_value(SecretId=name, SecretString="v2")
    current = sm.get_secret_value(SecretId=name)

    # Query and path forms of the same id share one entry.
    assert document(agent.get("/v1/" + name))["SecretString"] == "v1"
    assert recorder.take() == []
    # A different id string, or explicit version parameters, are separate entries.
    for params, expected in [
        ({"secret_id": arn}, current),
        ({"secret_id": name, "versionStage": "AWSCURRENT"}, current),
        ({"secret_id": name, "versionId": v1}, sm.get_secret_value(SecretId=name, VersionId=v1)),
        ({"secret_id": name, "versionStage": "AWSPREVIOUS"}, sm.get_secret_value(SecretId=name, VersionStage="AWSPREVIOUS")),
    ]:
        secret_id = params.pop("secret_id")
        assert_matches(document(agent.query(secret_id, **params)), expected)
        assert recorder.take() == ["GetSecretValue"], params
        assert_matches(document(agent.query(secret_id, **params)), expected)
        assert recorder.take() == [], params
    assert document(agent.query(name))["SecretString"] == "v1"
    assert recorder.take() == []


def test_failed_refresh_keeps_cached_entry(agent, recorder, secrets, sm):
    created = secrets.create("keep", SecretString="kept")
    name = created["Name"]
    first = document(agent.query(name))
    sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    status, body = service_error(lambda: sm.get_secret_value(SecretId=name))
    recorder.take()
    assert_error(agent.query(name, refreshNow="true"), status, body)
    assert recorder.take() == ["GetSecretValue"]
    assert_same_document(document(agent.query(name)), first)
    assert recorder.take() == []


def test_expiry_revalidates_unchanged_version(short, recorder, secrets, sm):
    # cache_behavior.rs::test_cache_expiration_and_refresh, plus the revalidation cost.
    created = secrets.create("revalidate", SecretString="same")
    name, v1 = created["Name"], created["VersionId"]
    first = document(short.query(name))
    assert first["VersionStages"] == ["AWSCURRENT"]
    assert recorder.take() == ["GetSecretValue"]
    # A new label on the cached version does not make it stale.
    sm.update_secret_version_stage(SecretId=name, VersionStage="blue", MoveToVersionId=v1)
    time.sleep(EXPIRE)
    assert_same_document(document(short.query(name)), first)
    assert recorder.take() == ["DescribeSecret"]
    # Revalidation renewed the entry.
    assert_same_document(document(short.query(name)), first)
    assert recorder.take() == []
    refreshed = document(short.query(name, refreshNow="true"))
    assert sorted(refreshed["VersionStages"]) == ["AWSCURRENT", "blue"]
    assert_matches(refreshed, sm.get_secret_value(SecretId=name))


def test_expiry_refetches_changed_version(short, recorder, secrets, sm):
    created = secrets.create("changed", SecretString="old")
    name = created["Name"]
    first = document(short.query(name))
    recorder.take()
    sm.put_secret_value(SecretId=name, SecretString="new")
    assert_same_document(document(short.query(name)), first)
    time.sleep(EXPIRE)
    fresh = document(short.query(name))
    assert fresh["SecretString"] == "new"
    assert_matches(fresh, sm.get_secret_value(SecretId=name))
    assert recorder.take() == ["DescribeSecret", "GetSecretValue"]
    assert_same_document(document(short.query(name)), fresh)
    assert recorder.take() == []


def test_expiry_of_version_and_stage_entries(short, recorder, secrets, sm):
    created = secrets.create("entries", SecretString="one")
    name, v1 = created["Name"], created["VersionId"]
    sm.put_secret_value(SecretId=name, SecretString="two")
    by_id = document(short.query(name, versionId=v1))
    previous = document(short.query(name, versionStage="AWSPREVIOUS"))
    assert by_id["VersionId"] == previous["VersionId"] == v1
    assert by_id["VersionStages"] == ["AWSPREVIOUS"]
    recorder.take()

    sm.put_secret_value(SecretId=name, SecretString="three")
    time.sleep(EXPIRE)
    # v1 lost its last label, so it no longer appears among the described versions: refetched.
    assert v1 not in sm.describe_secret(SecretId=name)["VersionIdsToStages"]
    unlabeled = document(short.query(name, versionId=v1))
    assert unlabeled["SecretString"] == "one" and "VersionStages" not in unlabeled
    assert_matches(unlabeled, sm.get_secret_value(SecretId=name, VersionId=v1))
    assert recorder.take() == ["DescribeSecret", "GetSecretValue"]
    # A stage entry is refetched once the label has moved to another version.
    moved = document(short.query(name, versionStage="AWSPREVIOUS"))
    assert moved["SecretString"] == "two"
    assert_matches(moved, sm.get_secret_value(SecretId=name, VersionStage="AWSPREVIOUS"))
    assert recorder.take() == ["DescribeSecret", "GetSecretValue"]


def test_version_id_entry_keeps_original_stages(short, recorder, secrets, sm):
    created = secrets.create("demoted", SecretString="one")
    name, v1 = created["Name"], created["VersionId"]
    cached = document(short.query(name, versionId=v1))
    assert cached["VersionStages"] == ["AWSCURRENT"]
    sm.put_secret_value(SecretId=name, SecretString="two")
    time.sleep(EXPIRE)
    recorder.take()
    assert_same_document(document(short.query(name, versionId=v1)), cached)
    assert recorder.take() == ["DescribeSecret"]
    assert sm.get_secret_value(SecretId=name, VersionId=v1)["VersionStages"] == ["AWSPREVIOUS"]


def test_ttl_zero_revalidates_every_request(launch, recorder, secrets, sm):
    # cache_behavior.rs::test_ttl_zero_disables_caching.
    created = secrets.create("zero", SecretString="v1")
    name = created["Name"]
    port = free_port()
    program = launch(nested_config(port, "[capabilities.secrets_manager.cache]\nttl_seconds = 0\n"), endpoint=recorder.url, port=port)
    recorder.take()
    first = document(program.query(name))
    assert recorder.take() == ["GetSecretValue"]
    for _ in range(3):
        assert_same_document(document(program.query(name)), first)
        assert recorder.take() == ["DescribeSecret"]
    v2 = sm.put_secret_value(SecretId=name, SecretString="v2")["VersionId"]
    fresh = document(program.query(name))
    assert fresh["VersionId"] == v2
    assert_matches(fresh, sm.get_secret_value(SecretId=name))
    assert recorder.take() == ["DescribeSecret", "GetSecretValue"]


def test_cached_secret_marked_for_deletion(short, recorder, secrets, sm):
    created = secrets.create("scheduled", SecretString="still-here")
    name = created["Name"]
    first = document(short.query(name))
    sm.delete_secret(SecretId=name, RecoveryWindowInDays=7)
    assert_same_document(document(short.query(name)), first)
    time.sleep(EXPIRE)
    recorder.take()
    # The deleted secret still describes its version as current, so the entry stays valid.
    assert_same_document(document(short.query(name)), first)
    assert recorder.take() == ["DescribeSecret"]
    status, body = service_error(lambda: sm.get_secret_value(SecretId=name))
    assert_error(short.query(name, refreshNow="true"), status, body)
    assert_same_document(document(short.query(name)), first)


def test_recreated_secret_with_same_name(short, recorder, secrets, sm):
    created = secrets.create("reborn", SecretString="first-life")
    name = created["Name"]
    document(short.query(name))
    sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
    reborn = sm.create_secret(Name=name, SecretString="second-life")
    assert reborn["ARN"] != created["ARN"]
    time.sleep(EXPIRE)
    recorder.take()
    # Revalidation describes the cached ARN, which no longer exists.
    status, body = service_error(lambda: sm.describe_secret(SecretId=created["ARN"]))
    for _ in range(2):
        assert_error(short.query(name), status, body)
        ops = recorder.take()
        assert ops and set(ops) == {"DescribeSecret"}
    fresh = document(short.query(name, refreshNow="true"))
    assert_matches(fresh, sm.get_secret_value(SecretId=name))
    assert_same_document(document(short.query(name)), fresh)


def test_eviction_by_write_order(launch, recorder, secrets, sm):
    names = [secrets.create(s, SecretString=f"{s}-1")["Name"] for s in ("a", "b", "c")]
    a, b, c = names
    port = free_port()
    program = launch(nested_config(port, "[capabilities.secrets_manager.cache]\nttl_seconds = 300\ncache_size = 2\n"), endpoint=recorder.url, port=port)
    recorder.take()

    def value(name, **params):
        return document(program.query(name, **params))["SecretString"]

    assert (value(a), value(b)) == ("a-1", "b-1")
    for name in names:
        sm.put_secret_value(SecretId=name, SecretString=name[-1] + "-2")
    assert recorder.take() == ["GetSecretValue"] * 2
    assert value(a) == "a-1"  # a read does not protect a from eviction
    assert recorder.take() == []
    assert value(c) == "c-2"  # cache now holds b, c
    assert value(b) == "b-1"
    assert recorder.take() == ["GetSecretValue"]
    assert value(a) == "a-2"  # refetched, evicting b: cache holds c, a
    assert recorder.take() == ["GetSecretValue"]
    assert value(c, refreshNow="true") == "c-2"  # rewritten entries move to the back: a, c
    assert recorder.take() == ["GetSecretValue"]
    assert value(b) == "b-2"  # evicts a: cache holds c, b
    assert value(c) == "c-2"
    assert recorder.take() == ["GetSecretValue"]
    assert value(a) == "a-2"
    assert recorder.take() == ["GetSecretValue"]


def test_rotation_label_moves(short, recorder, secrets, sm):
    # Adapted from integration-tests/tests/version_management.rs::test_version_stage_transitions.
    created = secrets.create("rotation", SecretString="v1")
    name, v1 = created["Name"], created["VersionId"]
    current = document(short.query(name))
    v2 = sm.put_secret_value(SecretId=name, SecretString="v2", VersionStages=["AWSPENDING"])["VersionId"]
    pending = document(short.query(name, versionStage="AWSPENDING"))
    assert pending["VersionId"] == v2 and pending["VersionStages"] == ["AWSPENDING"]
    assert_same_document(document(short.query(name)), current)

    sm.update_secret_version_stage(SecretId=name, VersionStage="AWSCURRENT", MoveToVersionId=v2, RemoveFromVersionId=v1)
    # Inside the TTL the old current and old pending documents are served unchanged.
    assert_same_document(document(short.query(name)), current)
    assert_same_document(document(short.query(name, versionStage="AWSPENDING")), pending)
    recorder.take()
    time.sleep(EXPIRE)

    promoted = document(short.query(name))
    assert promoted["VersionId"] == v2
    assert sorted(promoted["VersionStages"]) == ["AWSCURRENT", "AWSPENDING"]
    assert_matches(promoted, sm.get_secret_value(SecretId=name))
    assert recorder.take() == ["DescribeSecret", "GetSecretValue"]
    # The pending entry still names a version that holds AWSPENDING: revalidated, labels unchanged.
    assert_same_document(document(short.query(name, versionStage="AWSPENDING")), pending)
    assert recorder.take() == ["DescribeSecret"]
    previous = document(short.query(name, versionStage="AWSPREVIOUS"))
    assert previous["VersionId"] == v1
    assert_matches(previous, sm.get_secret_value(SecretId=name, VersionStage="AWSPREVIOUS"))
