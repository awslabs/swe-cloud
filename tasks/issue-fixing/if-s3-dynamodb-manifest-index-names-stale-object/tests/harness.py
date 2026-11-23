"""Role processes, direct-to-Floci observations and the row/object checks the tests share."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import boto3

from cdk_seed.cfn_deploy import deploy
from botocore.config import Config
from botocore.exceptions import ClientError

FLOCI = "http://127.0.0.1:4566"
APP = os.environ.get("STRATA_VERIFY_APP", "/app")
# The batch fleet's reader: the 0.14 release exactly as /app shipped it, kept beside the verifier.
LEGACY_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy014")
# Role slots: committer, reader-helper, sampling reader. Each slot has its own proxy port.
SLOT_W, SLOT_R, SLOT_R2 = 0, 1, 2

RESOLVE_DEADLINE_S = 20
COMMIT_DEADLINE_S = 20
HOLD_ENGAGE_TIMEOUT_S = 15
MANIFEST_BYTES = 2048
CORRUPT_SIZE_DELTA_BYTES = 7

KEY_ATTRS = {"base_uri", "version"}


def _client(service):
    return boto3.client(
        service,
        endpoint_url=FLOCI,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(s3={"addressing_style": "path"}, retries={"mode": "standard", "max_attempts": 5}),
    )


def manifest_bytes(committer, size=MANIFEST_BYTES):
    head = json.dumps({
        "format": "strata-manifest/1",
        "committer": committer,
        "nonce": uuid.uuid4().hex,
        "fragments": [{"id": i, "path": f"data/{uuid.uuid4().hex}.parquet"} for i in range(3)],
    })
    pad = (committer + "|") * (size // (len(committer) + 1) + 1)
    data = (head + "\n" + pad)[:size]
    return data.encode()


# ---------------------------------------------------------------------- world


class World:
    """One table + versioned bucket for the session; one dataset prefix per scenario."""

    def __init__(self, ports):
        self.ports = ports
        suffix = uuid.uuid4().hex[:8]
        self.table = f"strata-verify-{suffix}"
        self.bucket = f"strata-verify-{suffix}"
        self.ddb = _client("dynamodb")
        self.s3 = _client("s3")
        deploy(_client('cloudformation'), 'seed-' + suffix,
               Path(__file__).parent / 'cdk_seed/seed.template.json',
               TableName=self.table, BucketName=self.bucket)

    def dataset(self, label):
        return f"wh-{uuid.uuid4().hex[:6]}/{label}"

    def base_uri(self, dataset):
        return f"s3://{self.bucket}/{dataset}"

    @staticmethod
    def canonical(dataset, version):
        return f"{dataset}/_versions/{version}.manifest"

    # ------------------------------------------------------------------ observation

    def row(self, dataset, version):
        response = self.ddb.get_item(
            TableName=self.table,
            Key={"base_uri": {"S": self.base_uri(dataset)}, "version": {"N": str(version)}},
            ConsistentRead=True,
        )
        return response.get("Item")

    def sample(self, dataset, version):
        key = self.canonical(dataset, version)
        row = self.row(dataset, version)
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            head = None
        versions, markers = [], []
        paginator = self.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            versions += [v for v in page.get("Versions", []) if v["Key"] == key]
            markers += [m for m in page.get("DeleteMarkers", []) if m["Key"] == key]
        return Snapshot(dataset, version, key, row, head, versions, markers)

    def version_bytes(self, key, version_id):
        response = self.s3.get_object(Bucket=self.bucket, Key=key, VersionId=version_id)
        return response["Body"].read()

    def wait_row(self, dataset, version, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.row(dataset, version)
            if predicate(row):
                return row
            time.sleep(0.1)
        return None

    # ------------------------------------------------------------------ roles

    def env(self, slot, app=APP):
        env = dict(os.environ)
        env.update({
            "AWS_ENDPOINT_URL": f"http://127.0.0.1:{self.ports[slot]}",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "STRATA_TABLE": self.table,
            "STRATA_BUCKET": self.bucket,
            "PYTHONPATH": app,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.pop("AWS_ENDPOINT_URL_S3", None)
        env.pop("AWS_ENDPOINT_URL_DYNAMODB", None)
        return env

    def commit(self, slot, dataset, version, data, committer):
        workdir = tempfile.mkdtemp(prefix="strata-role-")
        path = os.path.join(workdir, "manifest.json")
        with open(path, "wb") as handle:
            handle.write(data)
        return Role(self, slot, workdir, [
            "commit", "--dataset", dataset, "--version", str(version),
            "--manifest-file", path, "--committer", committer,
        ])

    def resolve(self, slot, dataset, version=None, legacy=False):
        workdir = tempfile.mkdtemp(prefix="strata-role-")
        which = ["--latest"] if version is None else ["--version", str(version)]
        return Role(self, slot, workdir, ["resolve", "--dataset", dataset, *which],
                    app=LEGACY_APP if legacy else APP)


class Role:
    def __init__(self, world, slot, workdir, args, app=APP):
        self.args = args
        self.slot = slot
        self.app = app
        self.stdout_path = os.path.join(workdir, "stdout")
        self.stderr_path = os.path.join(workdir, "stderr")
        self._out = open(self.stdout_path, "wb")
        self._err = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(app, "stratactl"), *args],
            cwd=workdir, env=world.env(slot, app), stdout=self._out, stderr=self._err,
            stdin=subprocess.DEVNULL,
        )
        self.started = time.monotonic()

    def alive(self):
        return self.proc.poll() is None

    def wait(self, timeout):
        """Return the exit code, or None if the process is still running after `timeout`."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self):
        if self.alive():
            self.proc.kill()
            self.proc.wait()

    def stdout(self):
        self._out.flush()
        with open(self.stdout_path, "rb") as handle:
            return handle.read().decode(errors="replace")

    def stderr(self):
        self._err.flush()
        with open(self.stderr_path, "rb") as handle:
            return handle.read().decode(errors="replace")

    def output(self):
        """The last JSON object printed on stdout, or None."""
        for line in reversed(self.stdout().strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    return None
        return None

    def describe(self):
        release = " [0.14 batch-fleet reader]" if self.app == LEGACY_APP else ""
        return (f"`stratactl {' '.join(self.args)}`{release} (rc={self.proc.poll()})\n"
                f"stdout: {self.stdout()[-1500:]}\nstderr: {self.stderr()[-3000:]}")


# ---------------------------------------------------------------------- checks


def _strings(value):
    if isinstance(value, dict):
        for kind, inner in value.items():
            if kind in ("S", "N"):
                yield str(inner)
            elif kind in ("SS", "NS"):
                yield from (str(x) for x in inner)
            elif kind == "L":
                for x in inner:
                    yield from _strings(x)
            elif kind == "M":
                for x in inner.values():
                    yield from _strings(x)


@dataclass
class Snapshot:
    dataset: str
    version: int
    key: str
    row: Optional[dict]
    head: Optional[dict]
    versions: list
    markers: list = field(default_factory=list)

    @property
    def current_id(self):
        return self.head.get("VersionId") if self.head else None

    @property
    def version_ids(self):
        return {v["VersionId"] for v in self.versions}

    def row_path(self):
        return (self.row or {}).get("path", {}).get("S")

    def finalized(self):
        return self.row_path() == self.key

    def describe(self):
        current = self.current_id
        listed = ", ".join(
            f"{v['VersionId']}{' (current)' if v['VersionId'] == current else ''}"
            for v in self.versions
        )
        return f"row={self.row} head.VersionId={current} versions=[{listed}]"


def row_truth_problems(snap):
    """What the index row asserts about the canonical object must be true of it, right now."""
    problems = []
    row = snap.row
    if row is None:
        return problems
    path = snap.row_path()
    if path is None:
        return [f"index row has no string `path`: {snap.describe()}"]
    if path == snap.key:
        if snap.head is None:
            return [f"index row names {snap.key} but no object exists there: {snap.describe()}"]
        size = row.get("size", {}).get("N")
        if size is None:
            problems.append(f"finalized index row has no numeric `size`: {snap.describe()}")
        elif int(size) != snap.head["ContentLength"]:
            problems.append(
                f"index row size {size} != canonical ContentLength {snap.head['ContentLength']}: "
                f"{snap.describe()}"
            )
        if "generation" in row:
            recorded = row["generation"].get("S")
            if recorded != snap.current_id:
                problems.append(
                    "a reader of the deployed release refuses this version: the row records "
                    f"generation {recorded!r} but the canonical object is {snap.current_id!r}: "
                    f"{snap.describe()}"
                )
    current_tokens = {snap.current_id}
    if snap.head and snap.head.get("ETag"):
        current_tokens.add(snap.head["ETag"].strip('"'))
    stale = set()
    for v in snap.versions:
        if v["VersionId"] != snap.current_id:
            stale.add(v["VersionId"])
            stale.add(v.get("ETag", "").strip('"'))
    stale -= current_tokens
    stale.discard("")
    stale.discard(None)
    for name, value in row.items():
        if name in KEY_ATTRS:
            continue
        for text in _strings(value):
            for token in stale:
                if token and token in text:
                    problems.append(
                        f"index row attribute `{name}` names superseded object generation "
                        f"{token!r} of {snap.key}: {snap.describe()}"
                    )
    return problems


def bytes_problems(world, snap, expected):
    problems = []
    for v in snap.versions:
        data = world.version_bytes(snap.key, v["VersionId"])
        if data != expected:
            problems.append(
                f"canonical version {v['VersionId']} of {snap.key} holds {len(data)} bytes that are "
                f"not the selected manifest ({len(expected)} bytes)"
            )
    return problems


def generation_problems(label, generation, snap, must_be_current):
    if not isinstance(generation, str) or not generation:
        return [f"{label} printed generation {generation!r}; expected the VersionId it observed "
                f"at {snap.key}: {snap.describe()}"]
    if generation not in snap.version_ids:
        return [f"{label} printed generation {generation!r}, which {snap.key} never held: "
                f"{snap.describe()}"]
    if must_be_current and generation != snap.current_id:
        return [f"{label} printed generation {generation!r} but the canonical object it read is "
                f"{snap.current_id!r}: {snap.describe()}"]
    return []
