"""Drive one scenario: seed a table, start the gateways and the sidecar, run the two workers."""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from cdk_seed.cfn_deploy import deploy

import boto3
from botocore.config import Config

from .gateway import Rig, norm_etag, start_server
from .schedule import FaultPolicy
from .sidecar import Sidecar

APP_DIR = os.environ.get("LOCKWORKER_APP_DIR", "/app")
UPSTREAM_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://127.0.0.1:4566")
WORKER_PYTHON = os.environ.get("LOCKWORKER_PYTHON", "python3")


def _admin():
    return boto3.client("s3", endpoint_url=UPSTREAM_ENDPOINT,
                        config=Config(retries={"max_attempts": 3, "mode": "standard"}))


def seed_table(bucket, lock_key, ledger_prefix, results_prefix):
    s3 = _admin()
    buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if bucket not in buckets:
        deploy(boto3.client("cloudformation", endpoint_url=UPSTREAM_ENDPOINT),
               "lock-test-seed-" + bucket,
               Path(__file__).resolve().parents[1] / "cdk_seed/seed.template.json",
               BucketName=bucket)
    else:
        # Existing buckets are not adopted into a new stack.
        try:
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        except Exception:
            pass
    for prefix in (ledger_prefix, results_prefix):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=obj["Key"])
    seed_body = json.dumps({"owner": "seed", "expired": True, "validUntil": 0}).encode()
    resp = s3.put_object(Bucket=bucket, Key=lock_key, Body=seed_body, ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=f"{ledger_prefix}counter.json",
                  Body=json.dumps({"seq": 0}).encode(), ContentType="application/json")
    return norm_etag(resp["ETag"])


class Scenario:
    def __init__(self, *, bucket, lock_key, ledger_prefix, results_prefix, lease_seconds,
                 drift_buffer_ms, owners, sections, hold_ms, poll_ms, acquire_timeout_s,
                 faults, sidecar_hold_ms, release_fail_count, gate_timeout_s, ports):
        self.cfg = dict(bucket=bucket, lock_key=lock_key, ledger_prefix=ledger_prefix,
                        results_prefix=results_prefix, lease_seconds=lease_seconds,
                        drift_buffer_ms=drift_buffer_ms)
        self.ports = ports            # {"w1": port, "w2": port}
        self.owners = owners          # {"w1": owner, "w2": owner}
        self.sections = sections      # {"w1": n, "w2": n}
        self.hold_ms = hold_ms
        self.poll_ms = poll_ms
        self.acquire_timeout_s = acquire_timeout_s
        self.faults = faults
        self.sidecar_hold_ms = sidecar_hold_ms
        self.release_fail_count = release_fail_count
        self.gate_timeout_s = gate_timeout_s
        self.rig = None
        self._servers = []
        self._tmpdirs = []

    def __enter__(self):
        seed_etag = seed_table(self.cfg["bucket"], self.cfg["lock_key"],
                               self.cfg["ledger_prefix"], self.cfg["results_prefix"])
        host, _, port = UPSTREAM_ENDPOINT.split("//")[1].partition(":")
        self.rig = Rig(upstream=(host, int(port or 4566)), **self.cfg)
        self.rig.record_seed(seed_etag)
        self.rig.gate_timeout_s = self.gate_timeout_s
        if self.faults:
            self.rig.sidecar = Sidecar(self.rig, self.owners["w1"], self.sidecar_hold_ms,
                                       self.cfg["lease_seconds"] * 1000)
            self.rig.policy = FaultPolicy(self.rig, "w1", self.sections["w1"],
                                          self.release_fail_count, self.gate_timeout_s)
        for role, port in self.ports.items():
            self._servers.append(start_server(self.rig, role, port))
        return self

    def __exit__(self, *exc):
        for s in self._servers:
            s.shutdown()
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def _worker_cmd(self, role):
        c = self.cfg
        return [WORKER_PYTHON, os.path.join(APP_DIR, "lockworker"),
                "--worker-id", role, "--owner", self.owners[role],
                "--bucket", c["bucket"], "--lock-key", c["lock_key"],
                "--ledger-prefix", c["ledger_prefix"], "--results-prefix", c["results_prefix"],
                "--sections", str(self.sections[role]), "--lease-seconds", str(c["lease_seconds"]),
                "--hold-ms", str(self.hold_ms), "--poll-ms", str(self.poll_ms),
                "--acquire-timeout-s", str(self.acquire_timeout_s)]

    def _worker_env(self, role):
        env = dict(os.environ)
        env["AWS_ENDPOINT_URL"] = f"http://127.0.0.1:{self.ports[role]}"
        env["AWS_DEFAULT_REGION"] = "us-east-1"
        env["AWS_ACCESS_KEY_ID"] = "test"
        env["AWS_SECRET_ACCESS_KEY"] = "test"
        env["AWS_REQUEST_CHECKSUM_CALCULATION"] = "when_required"
        env["PYTHONPATH"] = APP_DIR
        return env

    def run(self, join_timeout_s):
        procs = {}
        for role in ("w1", "w2"):
            d = tempfile.mkdtemp(prefix=f"{role}-")
            self._tmpdirs.append(d)
            procs[role] = subprocess.Popen(self._worker_cmd(role), env=self._worker_env(role),
                                           cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + join_timeout_s
        out = {}
        for role, p in procs.items():
            remaining = max(1.0, deadline - time.monotonic())
            try:
                so, se = p.communicate(timeout=remaining)
                out[role] = (p.returncode, so.decode("utf-8", "replace"), se.decode("utf-8", "replace"))
            except subprocess.TimeoutExpired:
                p.kill()
                so, se = p.communicate()
                out[role] = (None, so.decode("utf-8", "replace"), se.decode("utf-8", "replace"))
        # A write the gateway is still holding resolves (lands or is refused) before grading.
        for t in list(self.rig.late_writes):
            t.join(timeout=30)
        return out


def read_json(bucket, key):
    s3 = _admin()
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def list_ledger_entries(bucket, ledger_prefix):
    s3 = _admin()
    entries = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{ledger_prefix}entries/"):
        for obj in page.get("Contents", []):
            entries.append((obj["Key"], obj["LastModified"],
                            json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())))
    return entries


def list_lock_versions(bucket, lock_key):
    """Every version of the lock object, oldest first: (etag, body)."""
    s3 = _admin()
    versions = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=lock_key):
        for v in page.get("Versions", []):
            if v["Key"] != lock_key:
                continue
            body = s3.get_object(Bucket=bucket, Key=lock_key, VersionId=v["VersionId"])["Body"].read()
            versions.append((v["LastModified"], norm_etag(v["ETag"]), json.loads(body)))
    versions.sort(key=lambda r: r[0])
    return [(etag, body) for _, etag, body in versions]
