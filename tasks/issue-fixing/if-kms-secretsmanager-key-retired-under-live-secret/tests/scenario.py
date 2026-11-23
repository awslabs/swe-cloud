"""Drives three sweeps against a seeded account and records what happened.

Nothing here imports from /app: the sweep is run as a subprocess, exactly the way the scheduler
runs it (fresh working directory, PYTHONPATH=/app, AWS_ENDPOINT_URL at the gateway).
"""
import base64
import hashlib
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

import boto3
from botocore.config import Config

from harness_gateway import PROTECTION_TAG, Gateway

FLOCI = os.environ.get("VERIFIER_FLOCI", "http://localhost:4566")
APP = os.environ.get("VERIFIER_APP", "/app")
RUN_DEADLINE_S = 120
THROTTLE_FAILS = 2
S3_FAULT_BUDGET = 12
EXIT_INCOMPLETE = 3


def direct_clients():
    cfg = Config(retries={"total_max_attempts": 3, "mode": "standard"}, s3={"addressing_style": "path"})
    kw = dict(endpoint_url=FLOCI, region_name="us-east-1", aws_access_key_id="test",
              aws_secret_access_key="test", config=cfg)
    return {s: boto3.client(s, **kw) for s in ("kms", "secretsmanager", "s3")}


def floci_live():
    try:
        with urllib.request.urlopen(FLOCI + "/_floci/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


class Consumer:
    def __init__(self, cid, kind, key, ref, plaintext):
        self.cid = cid            # consumer id (secret name or s3://bucket/key)
        self.kind = kind          # "secret" | "object"
        self.key = key            # label of the key it is encrypted under (K1..K5)
        self.ref = ref            # the key reference recorded with it
        self.plaintext = plaintext
        self.stored = None        # SecretString or object body as written
        self.arn = None
        self.bucket = None
        self.object_key = None
        self.created_ns = None
        self.removed_by_owner_ns = None


class SeamDriver:
    """Accepts pass_start announcements; runs the per-run action for a pass, then releases."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="seam-")
        self.path = os.path.join(self.dir, "seam.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(16)
        self.actions = {}
        self.announced = []
        self.errors = []
        threading.Thread(target=self._serve, daemon=True).start()

    def arm(self, actions):
        self.actions = dict(actions)
        self.announced = []

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        with conn:
            data = b""
            conn.settimeout(10)
            try:
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                msg = json.loads(data.decode() or "{}")
            except Exception:
                msg = {}
            self.announced.append(msg)
            if msg.get("seam") == "pass_start":
                action = self.actions.pop(msg.get("pass_no"), None)
                if action is not None:
                    try:
                        action()
                    except Exception:
                        self.errors.append(traceback.format_exc())
            try:
                conn.sendall(b"go\n")
            except OSError:
                pass


class Scenario:
    def __init__(self):
        self.error = None
        self.environment_ok = True
        self.runs = {}
        self.snapshots = {}
        self.keys = {}
        self.consumers = {}
        self.backup_bucket = None
        self.keep_bucket = None
        self.data_bucket = None
        self.gateway = None
        self.untagged_ns = None

    # -- seeding -----------------------------------------------------------------------------

    def seed(self):
        c = self.c
        r = secrets.token_hex(3)
        self.data_bucket, self.keep_bucket, self.backup_bucket = f"data-{r}", f"keep-{r}", f"backups-{r}"
        outputs = deploy(boto3.client("cloudformation", endpoint_url=FLOCI,
                                      region_name="us-east-1", aws_access_key_id="test",
                                      aws_secret_access_key="test"),
                         "kms-sandbox-" + r, Path(__file__).parent / "cdk_seed/seed.template.json",
                         RunId=r)
        for label in ("K1", "K2", "K3", "K4", "K5"):
            self.keys[label] = {"id": outputs[label + "Id"], "arn": outputs[label + "Arn"]}

        def ref(label, form):
            return self.keys[label]["arn" if form == "arn" else "id"]

        self._secret("S1", f"app/{r}/ledger-db", "K1", ref("K1", "arn"), protected=True)
        self._object("O1", self.data_bucket, f"exports/{r}/ledger-2026-10.bin", "K1", ref("K1", "id"))
        self._secret("S2", f"app/{r}/session-signing", "K2", ref("K2", "id"))
        self._object("O2", self.data_bucket, f"cache/{r}/sessions.bin", "K2", ref("K2", "arn"))
        self._object("O2B", self.data_bucket, f"cache/{r}/sessions-previous.bin", "K2", ref("K2", "id"))
        self._object("O4", self.keep_bucket, f"reference/{r}/golden.bin", "K4", ref("K4", "arn"))
        # O5 is written by a workload after the first sweep's scan (see run 1).
        self.late = ("O5", self.data_bucket, f"exports/{r}/late-writer.bin", "K3", ref("K3", "arn"))

    def _envelope(self, key_ref, cid):
        plaintext = secrets.token_bytes(32)
        ct = self.c["kms"].encrypt(KeyId=key_ref, Plaintext=plaintext,
                                   EncryptionContext={"consumer": cid})["CiphertextBlob"]
        return plaintext, ct

    def _secret(self, label, name, key, key_ref, protected=False):
        plaintext, ct = self._envelope(key_ref, name)
        con = Consumer(name, "secret", key, key_ref, plaintext)
        con.stored = base64.b64encode(ct).decode()
        out = deploy(boto3.client("cloudformation", endpoint_url=FLOCI,
                                  region_name="us-east-1", aws_access_key_id="test",
                                  aws_secret_access_key="test"),
                     "kms-secret-" + hashlib.sha256(name.encode()).hexdigest()[:24],
                     Path(__file__).parent / "cdk_seed/secret.template.json",
                     Name=name, KeyRef=key_ref, Value=con.stored,
                     Protected=str(protected).lower())
        con.arn = out["Arn"]
        con.created_ns = time.monotonic_ns()
        self.consumers[label] = con

    def _object(self, label, bucket, key, keylabel, key_ref):
        cid = f"s3://{bucket}/{key}"
        plaintext, ct = self._envelope(key_ref, cid)
        con = Consumer(cid, "object", keylabel, key_ref, plaintext)
        con.stored = ct
        con.bucket, con.object_key = bucket, key
        self.c["s3"].put_object(Bucket=bucket, Key=key, Body=ct, Metadata={"kms-key-id": key_ref})
        con.created_ns = time.monotonic_ns()
        self.consumers[label] = con

    def write_config(self):
        cfg = {
            "resource_types": ["S3EnvelopeObject", "SecretsManagerSecret", "KMSKey"],
            "filters": {"S3EnvelopeObject": [{"property": "Bucket", "value": self.keep_bucket},
                                             {"property": "Bucket", "value": self.backup_bucket}]},
            "backup_bucket": self.backup_bucket,
            "settings": {"KMSKey": {"pending_window_days": 7}},
            "pass_sleep_s": 1,
            "feature_flags": {"wait_on_dependencies": False},
        }
        d = tempfile.mkdtemp(prefix="sweep-config-")
        self.config_path = os.path.join(d, "sweep.json")
        with open(self.config_path, "w") as fh:
            json.dump(cfg, fh)

    # -- runs --------------------------------------------------------------------------------

    def run_sweep(self, label, throttle=None, s3_fault=None, seam_actions=None):
        self.gateway.set_run(label, throttle=throttle, s3_read_fault=s3_fault)
        self.seams.arm(seam_actions or {})
        cwd = tempfile.mkdtemp(prefix=f"sweep-{label}-")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
        env.update(AWS_ENDPOINT_URL=f"http://127.0.0.1:{self.port}", AWS_DEFAULT_REGION="us-east-1",
                   AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", PYTHONPATH=APP,
                   SEAM_SOCKET=self.seams.path, PYTHONDONTWRITEBYTECODE="1")
        log_path = os.path.join(cwd, "sweep.log")
        started = time.monotonic()
        with open(log_path, "wb") as log:
            proc = subprocess.Popen([sys.executable, os.path.join(APP, "nuke.py"), "run", "--config",
                                     self.config_path, "--no-dry-run"], cwd=cwd, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            killed = False
            try:
                rc = proc.wait(timeout=RUN_DEADLINE_S)
            except subprocess.TimeoutExpired:
                killed = True
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    proc.kill()
                proc.wait()
                rc = None
        elapsed = time.monotonic() - started
        self.gateway.set_run(None)
        with open(log_path, "rb") as fh:
            tail = fh.read().decode(errors="replace")[-6000:]
        self.runs[label] = {"rc": rc, "killed": killed, "elapsed": elapsed, "log_tail": tail,
                            "seams": list(self.seams.announced), "seam_errors": list(self.seams.errors)}
        self.snapshots[label] = self.snapshot()

    def write_late_consumer(self):
        label, bucket, key, keylabel, key_ref = self.late
        self._object(label, bucket, key, keylabel, key_ref)

    # -- observation -------------------------------------------------------------------------

    def key_state(self, label):
        try:
            return self.c["kms"].describe_key(KeyId=self.keys[label]["id"])["KeyMetadata"]["KeyState"]
        except Exception as err:
            return f"error: {err}"

    def consumer_view(self, con):
        """None if gone; otherwise a dict describing whether it is intact and still readable."""
        c = self.c
        view = {}
        try:
            if con.kind == "secret":
                try:
                    d = c["secretsmanager"].describe_secret(SecretId=con.arn)
                except c["secretsmanager"].exceptions.ResourceNotFoundException:
                    return None
                if d.get("DeletedDate"):
                    view["scheduled_for_deletion"] = True
                view["tags"] = {t["Key"]: t.get("Value") for t in d.get("Tags", [])}
                view["ref_intact"] = d.get("KmsKeyId") == con.ref
                value = c["secretsmanager"].get_secret_value(SecretId=con.arn)["SecretString"]
                view["bytes_intact"] = value == con.stored
                ct = base64.b64decode(value)
            else:
                try:
                    obj = c["s3"].get_object(Bucket=con.bucket, Key=con.object_key)
                except c["s3"].exceptions.NoSuchKey:
                    return None
                body = obj["Body"].read()
                view["ref_intact"] = obj.get("Metadata", {}).get("kms-key-id") == con.ref
                view["bytes_intact"] = body == con.stored
                ct = body
            try:
                pt = c["kms"].decrypt(CiphertextBlob=ct, EncryptionContext={"consumer": con.cid})["Plaintext"]
                view["decrypts"] = pt == con.plaintext
            except Exception as err:
                view["decrypts"] = False
                view["decrypt_error"] = str(err)
        except Exception as err:
            view["error"] = str(err)
        return view

    def backups(self):
        out = {}
        s3 = self.c["s3"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.backup_bucket):
            for obj in page.get("Contents", []):
                try:
                    rec = json.loads(s3.get_object(Bucket=self.backup_bucket, Key=obj["Key"])["Body"].read())
                except Exception:
                    continue
                if isinstance(rec, dict) and "consumer" in rec:
                    out.setdefault(rec["consumer"], []).append(rec)
        return out

    def snapshot(self):
        return {
            "keys": {label: self.key_state(label) for label in self.keys},
            "consumers": {label: self.consumer_view(con) for label, con in self.consumers.items()},
            "backups": self.backups(),
        }

    # -- the whole schedule ------------------------------------------------------------------

    def execute(self):
        if not floci_live():
            self.environment_ok = False
            self.error = "environment: the emulator is not answering on " + FLOCI
            return
        self.c = direct_clients()
        self.seed()
        self.write_config()
        host, port = FLOCI.split("//", 1)[1].split(":")
        self.gateway = Gateway(host, int(port), self.c)
        for con in self.consumers.values():
            if con.kind == "secret":
                self.gateway.secret_arns[con.arn] = con.cid
        self.port = self.gateway.start()
        self.seams = SeamDriver()

        s2 = self.consumers["S2"].cid
        self.run_sweep(
            "run1",
            throttle={s2: THROTTLE_FAILS},
            s3_fault={self.keep_bucket: S3_FAULT_BUDGET},
            seam_actions={1: self.write_late_consumer, 2: self.gateway.clear_s3_fault},
        )
        self.gateway.clear_s3_fault()

        # The owning team lifts deletion protection on S1 before the next sweep.
        s1 = self.consumers["S1"]
        self.c["secretsmanager"].untag_resource(SecretId=s1.arn, TagKeys=[PROTECTION_TAG])
        self.untagged_ns = time.monotonic_ns()
        self.run_sweep("run2")

        # The owner of the kept reference object removes it before the third sweep.
        o4 = self.consumers["O4"]
        self.c["s3"].delete_object(Bucket=o4.bucket, Key=o4.object_key)
        o4.removed_by_owner_ns = time.monotonic_ns()
        self.run_sweep("run3")
        self.gateway.stop()


_SCENARIO = None


def get_scenario():
    global _SCENARIO
    if _SCENARIO is None:
        _SCENARIO = Scenario()
        try:
            _SCENARIO.execute()
        except Exception:
            _SCENARIO.error = traceback.format_exc()
    return _SCENARIO
