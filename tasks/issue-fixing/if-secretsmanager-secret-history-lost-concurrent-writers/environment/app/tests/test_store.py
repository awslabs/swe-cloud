import json
import uuid

import pytest
from botocore.exceptions import ClientError

from chambersm.store import METADATA_KEY, SecretNotFound, SecretsManagerStore


def _not_found():
    return ClientError({"Error": {"Code": "ResourceNotFoundException",
                                  "Message": "Secrets Manager can't find the specified secret."}},
                       "GetSecretValue")


class FakeSecretsManager:
    """In-memory stand-in for the boto3 client: every version ever written stays listed."""

    def __init__(self):
        self.secrets = {}   # name -> {"current": version_id, "versions": {id: SecretString}}

    def create_secret(self, Name, SecretString, **_):
        version_id = uuid.uuid4().hex
        self.secrets[Name] = {"current": version_id, "versions": {version_id: SecretString}}
        return {"Name": Name, "VersionId": version_id}

    def put_secret_value(self, SecretId, SecretString, **_):
        if SecretId not in self.secrets:
            raise _not_found()
        version_id = uuid.uuid4().hex
        secret = self.secrets[SecretId]
        secret["versions"][version_id] = SecretString
        secret["current"] = version_id
        return {"Name": SecretId, "VersionId": version_id}

    def get_secret_value(self, SecretId, VersionId=None, **_):
        if SecretId not in self.secrets:
            raise _not_found()
        secret = self.secrets[SecretId]
        version_id = VersionId or secret["current"]
        if version_id not in secret["versions"]:
            raise _not_found()
        return {"Name": SecretId, "VersionId": version_id,
                "SecretString": secret["versions"][version_id]}

    def describe_secret(self, SecretId, **_):
        if SecretId not in self.secrets:
            raise _not_found()
        return {"Name": SecretId, "RotationEnabled": False}

    def list_secret_version_ids(self, SecretId, **_):
        if SecretId not in self.secrets:
            raise _not_found()
        return {"Versions": [{"VersionId": v} for v in self.secrets[SecretId]["versions"]]}

    def update_secret_version_stage(self, **_):
        return {}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("CHAMBER_WRITER_ID", "unit-test")
    return SecretsManagerStore(client=FakeSecretsManager())


def test_write_creates_secret_and_reads_back(store):
    store.write("svc", "mykey", "value")
    secret = store.read("svc", "mykey")
    assert secret["value"] == "value"
    assert secret["version"] == 1
    assert secret["created_by"] == "unit-test"


def test_write_increments_key_version(store):
    store.write("svc", "mykey", "one")
    store.write("svc", "mykey", "two")
    assert store.read("svc", "mykey")["version"] == 2
    assert store.read("svc", "mykey", 1)["value"] == "one"
    assert store.read("svc", "mykey", 2)["value"] == "two"


def test_versions_are_per_key(store):
    store.write("svc", "a", "1")
    store.write("svc", "a", "2")
    store.write("svc", "b", "1")
    assert store.read("svc", "a")["version"] == 2
    assert store.read("svc", "b")["version"] == 1


def test_history(store):
    store.write("svc", "mykey", "one")
    store.write("svc", "other", "x")
    store.write("svc", "mykey", "two")
    events = store.history("svc", "mykey")
    assert [(e["event"], e["version"], e["value"]) for e in events] == [
        ("Created", 1, "one"), ("Updated", 2, "two")]


def test_history_missing_key(store):
    store.write("svc", "mykey", "one")
    with pytest.raises(SecretNotFound):
        store.history("svc", "nope")


def test_storage_format(store):
    store.write("svc", "mykey", "one")
    store.write("svc", "other", "two")
    raw = store.svc.get_secret_value(SecretId="svc")["SecretString"]
    doc = json.loads(raw)
    assert set(doc) == {"mykey", "other", METADATA_KEY}
    metadata = json.loads(doc[METADATA_KEY])
    assert metadata["mykey"]["version"] == 1
    assert metadata["other"]["created_by"] == "unit-test"


def test_delete(store):
    store.write("svc", "mykey", "one")
    store.write("svc", "other", "two")
    store.delete("svc", "mykey")
    with pytest.raises(SecretNotFound):
        store.read("svc", "mykey")
    assert store.list_raw("svc") == {"other": "two"}


def test_list(store):
    store.write("svc", "b", "2")
    store.write("svc", "a", "1")
    assert [e["key"] for e in store.list("svc")] == ["a", "b"]
    assert store.list("svc", include_values=True)[0]["value"] == "1"


def test_numbers_and_booleans_from_foreign_secrets(store):
    store.svc.create_secret(Name="rds", SecretString=json.dumps({"port": 5432, "ssl": True}))
    assert store.list_raw("rds") == {"port": "5432", "ssl": "true"}
