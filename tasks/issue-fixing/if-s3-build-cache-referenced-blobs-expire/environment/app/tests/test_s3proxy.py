"""Unit tests for cachesvc.s3proxy against a fake S3 client (port of upstream s3proxy_test.go)."""

import datetime
import threading
import time

import pytest
from botocore.exceptions import ClientError

from cachesvc.s3proxy import AC, CAS, S3Cache, object_key

BUCKET = "bazel-remote"
HASH = "a" * 64


def test_object_key():
    assert object_key("", "1234", CAS) == "cas.v2/12/1234"
    assert object_key("test", "1234", CAS) == "test/cas.v2/12/1234"
    assert object_key("", "1234", AC) == "ac/12/1234"
    assert object_key("foo/bar", "1234", AC) == "foo/bar/ac/12/1234"


class FakeClient:
    """Serves HEAD, GET and copy-onto-itself, and records the keys that were copied."""

    def __init__(self):
        self.mu = threading.Lock()
        self.modified = {}
        self.copied = []
        self.copy_gate = None  # when set, copies wait for it

    def _lm(self, key):
        with self.mu:
            if key not in self.modified:
                raise ClientError({"Error": {"Code": "404"},
                                   "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
            return datetime.datetime.fromtimestamp(self.modified[key], datetime.timezone.utc)

    def head_object(self, Bucket, Key):
        return {"LastModified": self._lm(Key), "ContentLength": 3}

    def get_object(self, Bucket, Key):
        lm = self._lm(Key)

        class Body:
            def read(self):
                return b"abc"

        return {"LastModified": lm, "ContentLength": 3, "Body": Body()}

    def copy_object(self, Bucket, Key, CopySource, **kwargs):
        if self.copy_gate is not None:
            self.copy_gate.wait()
        with self.mu:
            self.copied.append(Key)
            self.modified[Key] = time.time()
        return {"CopyObjectResult": {}}


def new_cache(client, age):
    return S3Cache(endpoint=None, bucket=BUCKET, cas_refresh_age=age, client=client)


def wait_for_copies(client, n, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with client.mu:
            if len(client.copied) >= n:
                return
        time.sleep(0.01)


def test_contains_refreshes_old_cas_blob():
    client = FakeClient()
    old, young = "1" * 64, "2" * 64
    client.modified[object_key("", old, CAS)] = time.time() - 3600
    client.modified[object_key("", young, CAS)] = time.time()
    cache = new_cache(client, 60)

    assert cache.contains(CAS, old)[0]
    assert cache.contains(CAS, young)[0]
    wait_for_copies(client, 1)
    time.sleep(0.1)
    assert client.copied == [object_key("", old, CAS)]


def test_ac_entries_are_never_refreshed():
    client = FakeClient()
    client.modified[object_key("", HASH, AC)] = time.time() - 3600
    cache = new_cache(client, 60)

    assert cache.contains(AC, HASH)[0]
    assert cache.get(AC, HASH) == b"abc"
    time.sleep(0.2)
    assert client.copied == []


def test_just_refreshed_blob_is_not_copied_again():
    client = FakeClient()
    key = object_key("", HASH, CAS)
    client.modified[key] = time.time() - 3600
    cache = new_cache(client, 60)

    cache.contains(CAS, HASH)
    wait_for_copies(client, 1)
    cache.contains(CAS, HASH)
    time.sleep(0.2)
    assert client.copied == [key]


def test_concurrent_lookups_produce_one_copy():
    client = FakeClient()
    key = object_key("", HASH, CAS)
    client.modified[key] = time.time() - 3600
    client.copy_gate = threading.Event()
    cache = new_cache(client, 60)

    threads = [threading.Thread(target=cache.contains, args=(CAS, HASH)) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    client.copy_gate.set()
    wait_for_copies(client, 1)
    time.sleep(0.2)
    assert client.copied == [key]


def test_get_refreshes():
    client = FakeClient()
    key = object_key("", HASH, CAS)
    client.modified[key] = time.time() - 3600
    cache = new_cache(client, 60)

    assert cache.get(CAS, HASH) == b"abc"
    wait_for_copies(client, 1)
    assert client.copied == [key]


@pytest.mark.parametrize("age", [0])
def test_zero_disables(age):
    client = FakeClient()
    client.modified[object_key("", HASH, CAS)] = time.time() - 3600
    cache = new_cache(client, age)

    cache.contains(CAS, HASH)
    cache.get(CAS, HASH)
    time.sleep(0.2)
    assert client.copied == []
