import base64
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time

import boto3
from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

APP = Path("/app")
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
LOGS = Path("/logs/verifier")
SER, DES = TypeSerializer(), TypeDeserializer()


def enc(item):
    return {key: SER.serialize(value) for key, value in item.items()}


def dec(item):
    result = {key: DES.deserialize(value) for key, value in item.items()}
    return {key: bytes(value) if hasattr(value, "value") and isinstance(value.value, bytes) else value
            for key, value in result.items()}


def clients():
    args = dict(endpoint_url=ENDPOINT, region_name="us-east-1",
                aws_access_key_id="test", aws_secret_access_key="test",
                config=Config(connect_timeout=2, read_timeout=5,
                              retries={"max_attempts": 1, "mode": "standard"}))
    return boto3.client("dynamodb", **args), boto3.client("kms", **args)


def read(ddb, table, key):
    item = ddb.get_item(TableName=table, Key=enc(key), ConsistentRead=True).get("Item")
    return dec(item) if item else None


def scan(ddb):
    args = {"TableName": "seal_entries", "ConsistentRead": True}
    items = []
    while True:
        page = ddb.scan(**args)
        items.extend(dec(item) for item in page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        args["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def env(endpoint):
    return {**os.environ, "AWS_ENDPOINT_URL": endpoint, "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "PYTHONPATH": str(APP), "PYTHONDONTWRITEBYTECODE": "1"}


class Processes:
    def __init__(self, label):
        self.label = label
        self.processes = []
        self.files = []
        self.directories = []

    def start(self, args, endpoint, role):
        directory = tempfile.TemporaryDirectory(prefix="seal-role-")
        self.directories.append(directory)
        logfile = (LOGS / f"{self.label}-{role}.stderr").open("a")
        self.files.append(logfile)
        proc = subprocess.Popen([sys.executable, "-m", "seal", *args], cwd=directory.name,
                                env=env(endpoint), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=logfile, text=True, bufsize=1)
        self.processes.append(proc)
        return proc

    def close(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in [proc.stdin, proc.stdout]:
                if stream:
                    stream.close()
        for file in self.files:
            file.close()
        for directory in self.directories:
            directory.cleanup()


class Writer:
    def __init__(self, proc, ledger, ledger_lock):
        self.proc = proc
        self.lock = threading.Lock()
        self.ledger = ledger
        self.ledger_lock = ledger_lock
        self.sequence = 0
        self.durations = []
        self.requests = []

    def put(self, identity, value, timeout=5, req=None, record=True):
        started = time.monotonic()
        with self.lock:
            self.sequence += 1
            req = req or f"{self.proc.pid}-{self.sequence}"
            request = {"req": req, "op": "put", "id": identity,
                       "value_b64": base64.b64encode(value).decode()}
            self.proc.stdin.write(json.dumps(request) + "\n")
            self.proc.stdin.flush()
            if not select.select([self.proc.stdout], [], [], timeout)[0]:
                raise AssertionError(f"put {identity}: no ack in {timeout}s")
            line = self.proc.stdout.readline()
            assert line, f"writer exited {self.proc.poll()} during put {identity}"
            response = json.loads(line)
            assert response.get("req") == req and response.get("ok") is True, response
            if record:
                with self.ledger_lock:
                    self.ledger[identity] = value
            self.requests.append(request)
            self.durations.append((identity, time.monotonic() - started))
            return request


def get(identity):
    # Deliberately execute the service interface; no imports from the submitted app.
    with tempfile.TemporaryDirectory(prefix="seal-read-") as directory:
        result = subprocess.run([sys.executable, "-m", "seal", "get", identity],
                                env=env(ENDPOINT), cwd=directory, capture_output=True,
                                text=True, timeout=5)
    assert result.returncode == 0, (identity, result.returncode, result.stderr)
    return base64.b64decode(result.stdout.strip(), validate=True)


def independent_value(kms, item):
    response = kms.decrypt(CiphertextBlob=item["wrapped_dek"])
    value = AESGCM(response["Plaintext"]).decrypt(item["iv"], item["ct"], None)
    return response["KeyId"], value
