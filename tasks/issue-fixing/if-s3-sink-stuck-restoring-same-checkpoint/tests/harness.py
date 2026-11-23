"""Verifier-side job manager and checker. Independent of /app: own record stream, own retention,
own manifests, own audits read straight from S3."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3

from cdk_seed.cfn_deploy import deploy
import botocore.exceptions
from botocore.config import Config

RECORD_BYTES = 1024
MIN_PART_BYTES = 5 * 1024 * 1024
RETAINED = 2
MAX_SNAPSHOT_BYTES = 4096
RESTORE_TIMEOUT_S = 30                  # the job manager waits as long for RESTORED as for any reply
REPLY_TIMEOUT_S = 30
EXIT_TIMEOUT_S = 10
CANCEL_GRACE_S = 10                     # stdin closed -> SIGTERM -> SIGKILL, as README says
FREEZE_AFTER_SNAPSHOT_S = 5             # a frozen host stays frozen well past the live attempt's snapshot
APP = os.environ.get("VERIFIER_APP_ROOT", "/app")
LOG_DIR = "/logs/verifier" if os.path.isdir("/logs/verifier") else tempfile.mkdtemp(prefix="verifier-logs-")
_PRINTABLE = bytes(33 + (b % 94) for b in range(256))


def s3_client():
    return boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                        region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test",
                        config=Config(retries={"max_attempts": 5, "mode": "standard"}))


class Records:
    def __init__(self, seed: str):
        self.seed = seed
        self._cache: dict = {}

    def one(self, i: int) -> bytes:
        return hashlib.shake_128(f"{self.seed}/{i}".encode()).digest(RECORD_BYTES - 1).translate(_PRINTABLE) + b"\n"

    def span(self, start: int, end: int) -> bytes:
        return b"".join(self._cache[i] if i in self._cache else self._cache.setdefault(i, self.one(i))
                        for i in range(start, end))

    def byte_range(self, lo: int, hi: int) -> bytes:
        first, last = lo // RECORD_BYTES, -(-hi // RECORD_BYTES)
        blob = self.span(first, last)
        return blob[lo - first * RECORD_BYTES: hi - first * RECORD_BYTES]


class ProtocolError(Exception):
    pass


class Attempt:
    def __init__(self, run: "Run", attempt: int, restore_from: str | None, label: str | None = None,
                 stalled: bool = False):
        self.run = run
        self.attempt = attempt
        self.label = label or f"a{attempt}"
        workdir = tempfile.mkdtemp(prefix=f"sink-{self.label}-")
        cmd = [sys.executable, os.path.join(APP, "sink.py"), "--bucket", run.bucket, "--job", run.job, "--attempt", str(attempt)]
        if restore_from:
            cmd += ["--restore-from", restore_from]
        if stalled:
            # The host froze right after the attempt was deployed: the process is stopped before
            # it has run a single line of the sink, and carries on only when thawed.
            cmd = ["/bin/sh", "-c", 'kill -STOP $$; exec "$0" "$@"'] + cmd
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
        env.update(PYTHONPATH=APP, TMPDIR=workdir, HOME=workdir,
                   AWS_ENDPOINT_URL=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                   AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")
        self.stderr = open(os.path.join(LOG_DIR, f"sink-{self.label}.log"), "wb")
        self.proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        run.attempts.append(self)
        if stalled:
            deadline = time.monotonic() + 10
            while self._state() != "T":
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    raise RuntimeError(f"{self.label}: could not stall the process")
                time.sleep(0.01)

    def _state(self) -> str:
        try:
            with open(f"/proc/{self.proc.pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return "?"

    def thaw(self):
        os.kill(self.proc.pid, signal.SIGCONT)

    def answer(self, timeout: float):
        """The first line the process prints within `timeout`, or None (exited or silent)."""
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def _pump(self):
        try:
            for raw in self.proc.stdout:
                self.lines.put(raw.decode(errors="replace").rstrip("\n"))
        finally:
            self.lines.put(None)

    def send(self, data: bytes):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise ProtocolError(f"{self.label}: stdin closed ({e})")

    def expect(self, word: str, timeout: float = REPLY_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise ProtocolError(f"{self.label}: no {word} within {timeout}s")
            if line is None:
                raise ProtocolError(f"{self.label}: exited ({self.proc.wait()}) while waiting for {word}")
            head, _, rest = line.partition(" ")
            if head == word:
                return rest
            if line.strip():
                raise ProtocolError(f"{self.label}: expected {word}, got {line[:160]!r}")

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.stderr.close()

    def cancel(self):
        """A superseded attempt is told to stop: stdin closed, SIGTERM after a grace, then SIGKILL."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=CANCEL_GRACE_S)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                return self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return None
        finally:
            self.kill()

    def wait_exit(self, timeout: float):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        finally:
            self.kill()


class Run:
    """One job in a fresh bucket, plus a neighbouring job's state in the same bucket."""

    def __init__(self):
        self.s3 = s3_client()
        tag = uuid.uuid4().hex[:8]
        self.bucket = f"archive-{tag}"
        self.job = f"order-events-{tag[:4]}"
        self.other = f"payments-audit-{tag[4:]}"
        self.records = Records(uuid.uuid4().hex)
        self.base = 3 + int(tag[:2], 16) % 50          # first checkpoint id of this job
        self.manifests: dict = {}                       # key -> {"offset", "state", "body"} present by our retention
        self.audits: list = []                          # (label, [problems])
        self.restores: dict = {}                        # label -> (expected id, outcome or None)
        self.errors: list = []
        self.attempts: list = []
        self.cancel_checks: dict = {}                   # label -> problems with the running attempt's state
        self.uploads: dict = {}
        self.live_upload: str | None = None             # the running attempt's upload, before any manifest names it
        self.completed_schedule = False
        self.final: dict = {}
        cfn = boto3.client('cloudformation', endpoint_url=self.s3.meta.endpoint_url,
                           region_name=self.s3.meta.region_name)
        deploy(cfn, 'seed-' + self.bucket,
               Path(__file__).parent / 'cdk_seed/seed.template.json', BucketName=self.bucket)

    # ---- job manager side ---------------------------------------------------------------

    def k(self, i: int) -> int:
        return self.base + i - 1

    def chk_key(self, cid: int) -> str:
        return f"jobs/{self.job}/chk/{cid:08d}.json"

    def write_records(self, a: Attempt, start: int, end: int, batch: int = 1000):
        pos = start
        while pos < end:
            n = min(batch, end - pos)
            a.send(f"WRITE {n}\n".encode() + self.records.span(pos, pos + n))
            pos += n
            got = a.expect("WROTE")
            if got.strip() != str(pos):
                raise ProtocolError(f"{a.label}: WROTE {got!r}, expected {pos}")

    def snapshot(self, a: Attempt, cid: int, offset: int) -> dict:
        a.send(f"CHECKPOINT {cid}\n".encode())
        rest = a.expect("SNAPSHOT")
        if len(rest.encode()) > MAX_SNAPSHOT_BYTES:
            raise ProtocolError(f"checkpoint {cid}: snapshot of {len(rest)} bytes declined")
        sid, _, payload = rest.partition(" ")
        state = json.loads(payload)
        if int(sid) != cid or int(state.get("offset", -1)) != offset:
            raise ProtocolError(f"checkpoint {cid}: snapshot id/offset {sid}/{state.get('offset')} != {cid}/{offset}")
        return state

    def write_manifest(self, cid: int, attempt: int, state: dict, offset: int):
        key = self.chk_key(cid)
        body = json.dumps({"job": self.job, "checkpoint_id": cid, "attempt": attempt, "state": state}).encode()
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        self.manifests[key] = {"offset": offset, "state": state, "body": body, "cid": cid}
        chk = sorted((m["cid"], k) for k, m in self.manifests.items() if "/chk/" in k)
        for _, old in chk[:-RETAINED]:
            self.s3.delete_object(Bucket=self.bucket, Key=old)
            del self.manifests[old]

    def take_savepoint(self, cid: int):
        src = self.manifests[self.chk_key(cid)]
        doc = json.loads(src["body"])
        doc["savepoint"] = True
        body = json.dumps(doc).encode()
        key = f"jobs/{self.job}/savepoints/sp-{cid:08d}-{uuid.uuid4().hex[:6]}.json"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        self.manifests[key] = {"offset": src["offset"], "state": src["state"], "body": body, "cid": cid}

    def complete(self, a: Attempt, cid: int):
        a.send(f"COMPLETE {cid}\n".encode())
        got = a.expect("COMPLETED")
        if got.strip() != str(cid):
            raise ProtocolError(f"COMPLETED {got!r}, expected {cid}")

    def checkpoint(self, a: Attempt, cid: int, offset: int):
        state = self.snapshot(a, cid, offset)
        self.write_manifest(cid, a.attempt, state, offset)
        self.complete(a, cid)
        self.audit(f"after COMPLETED {cid}")

    def restore(self, attempt: int, cid: int, label: str | None = None) -> Attempt | None:
        label = label or f"a{attempt}"
        self.audit(f"before restore {label}")
        a = Attempt(self, attempt, self.chk_key(cid), label)
        try:
            got = a.expect("RESTORED", timeout=RESTORE_TIMEOUT_S)
            ok = got.strip() == str(cid)
            self.restores[label] = (cid, got.strip())
        except ProtocolError as e:
            self.restores[label] = (cid, None)
            self.errors.append(f"restore {label} from checkpoint {cid}: {e}")
            a.kill()
            return None
        if not ok:
            self.errors.append(f"restore {label}: RESTORED {got!r}, expected {cid}")
            a.kill()
            return None
        return a

    def fresh(self, attempt: int) -> Attempt:
        a = Attempt(self, attempt, None)
        self.uploads[attempt] = a.expect("STARTED", timeout=RESTORE_TIMEOUT_S).strip()
        return a

    def open_ok(self, a: Attempt):
        a.send(b"OPEN ok\n")
        a.expect("OPENED")

    # ---- the schedule -------------------------------------------------------------------

    def seed_other_job(self):
        o = f"jobs/{self.other}"
        tail = f"{o}/out/_part-0_tmp_{uuid.uuid4()}"
        self.other_objects = {
            tail: os.urandom(70_000),
            f"{o}/out/_part-0_tmp_{uuid.uuid4()}": os.urandom(1000),
            f"{o}/chk/00000031.json": json.dumps({"job": self.other, "checkpoint_id": 31, "attempt": 4, "state": {
                "key": f"{o}/out/part-0", "upload_id": str(uuid.uuid4()), "parts": [], "bytes_in_parts": 0,
                "tail_key": tail, "tail_len": 70_000, "offset": 68}}).encode(),
        }
        for key, body in self.other_objects.items():
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)

    def schedule(self):
        k = self.k
        self.seed_other_job()

        # a0: fresh start, cut off from the job manager before its first checkpoint; it is still
        # running when a2 starts and is cancelled only once a2 is under way.
        a0 = self.fresh(0)
        self.open_ok(a0)
        self.write_records(a0, 0, 500, batch=500)

        # a1: deployed as a fresh start, but its host froze before the process ran anything; the job
        # manager declares the attempt failed and starts a2. a1 stays frozen until long after.
        a1 = Attempt(self, 1, None, stalled=True)

        # a2: fresh start, checkpoints k1..k4, a savepoint of k2, checkpoint k5 never completes.
        a = self.fresh(2)
        if self.uploads[2] == self.uploads[0]:
            raise ProtocolError("fresh attempts reported the same upload id")
        self.live_upload = self.uploads[2]
        self.open_ok(a)
        self.write_records(a, 0, 1000)
        a0.cancel()
        self.audit("after a0 cancelled")
        self.cancel_checks["a0 cancelled while a2 runs"] = (
            [] if self.uploads[2] in {u for _, u in self.list_uploads()}
            else [f"a2's upload {self.uploads[2]} is no longer open"])
        for i in range(1, 5):
            self.write_records(a, max((i - 1) * 2000, 1000), i * 2000)
            self.checkpoint(a, k(i), i * 2000)
            if i == 2:
                self.take_savepoint(k(2))
                self.audit(f"after savepoint of {k(2)}")
        self.write_records(a, 8000, 9000)
        self.snapshot(a, k(5), 9000)
        a.kill()

        # a3: restore k4; another operator fails to open.
        a = self.restore(3, k(4))
        if a is None:
            return
        a.send(b"OPEN fail\n")
        a.wait_exit(EXIT_TIMEOUT_S)

        # a4: restore k4 again; opened, ran, then cut off from the job manager (still running).
        a4 = self.restore(4, k(4))
        if a4 is None:
            return
        self.open_ok(a4)
        self.write_records(a4, 8000, 9000)

        # a5: deployed to restore k4, but its host froze before the process ran anything; the job
        # manager declares the attempt failed and starts a6. a5 is thawed while a6's snapshot of k6
        # is in flight, carries on with its startup, and is cancelled along with a4.
        self.audit("before restore a5")
        a5 = Attempt(self, 5, self.chk_key(k(4)), stalled=True)

        # a6: restore k4 a third time; k6 completes but its notification is delivered only after
        # the sink has snapshotted k7; k7's manifest is written; then a6's host is lost outright.
        a = self.restore(6, k(4))
        if a is None:
            a4.kill()
            a5.kill()
            return
        self.open_ok(a)
        self.write_records(a, 8000, 10000)
        state6 = self.snapshot(a, k(6), 10000)
        time.sleep(FREEZE_AFTER_SNAPSHOT_S)
        a5.thaw()
        a5.answer(RESTORE_TIMEOUT_S)                    # RESTORED, an exit, or nothing: not graded
        self.audit("after a5 resumed")
        a4.cancel()
        a5.cancel()
        self.write_manifest(k(6), 6, state6, 10000)
        self.cancel_checks["stale a4 and a5 while a6's snapshot is in flight"] = list(
            self.check_manifest(self.chk_key(k(6)), self.manifests[self.chk_key(k(6))]))
        self.audit(f"after manifest {k(6)}")
        self.write_records(a, 10000, 12000)
        state7 = self.snapshot(a, k(7), 12000)
        self.complete(a, k(6))
        self.audit(f"after late COMPLETED {k(6)}")
        self.write_manifest(k(7), 6, state7, 12000)
        self.audit(f"after manifest {k(7)}")
        a.kill()

        # The job is resubmitted by hand from the newest retained checkpoint (a deploy); the new
        # submission numbers its attempts from 0. While its snapshot of k8 is in flight, a1's host
        # comes back: a1 carries on with its fresh start, and then the host is lost for good. The
        # job runs to the end; it finishes right after k9 completes and the notification for k9 is
        # never delivered.
        a = self.restore(0, k(7), label="s2a0")
        if a is None:
            a1.kill()
            return
        self.open_ok(a)
        self.write_records(a, 12000, 14000)
        state8 = self.snapshot(a, k(8), 14000)
        time.sleep(FREEZE_AFTER_SNAPSHOT_S)
        a1.thaw()
        line = a1.answer(RESTORE_TIMEOUT_S)             # STARTED, an exit, or nothing: not graded
        if line and line.startswith("STARTED "):
            self.uploads[1] = line.split(" ", 1)[1].strip()
        a1.kill()
        self.audit("after a1 resumed and was lost")
        self.write_manifest(k(8), 0, state8, 14000)
        self.complete(a, k(8))
        self.audit(f"after COMPLETED {k(8)}")
        self.write_records(a, 14000, 16000)
        state9 = self.snapshot(a, k(9), 16000)
        self.write_manifest(k(9), 0, state9, 16000)
        self.audit(f"after manifest {k(9)}")
        a.send(b"END\n")
        rc = a.wait_exit(REPLY_TIMEOUT_S)
        if rc != 0:
            self.errors.append(f"s2a0 exit status after END: {rc}")
        self.completed_schedule = True

    # ---- checker ------------------------------------------------------------------------

    def list_keys(self, prefix: str = ""):
        out = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out[o["Key"]] = o
        return out

    def list_uploads(self):
        found, kw = [], {"Bucket": self.bucket}
        while True:
            page = self.s3.list_multipart_uploads(**kw)
            found += [(u["Key"], u["UploadId"]) for u in page.get("Uploads", [])]
            if not page.get("IsTruncated"):
                return found
            kw.update(KeyMarker=page.get("NextKeyMarker", ""), UploadIdMarker=page.get("NextUploadIdMarker", ""))

    def audit(self, label: str):
        problems = []
        try:
            if self.live_upload and self.live_upload not in {u for _, u in self.list_uploads()}:
                problems.append(f"upload {self.live_upload} of the running attempt is no longer open")
            for key, m in sorted(self.manifests.items()):
                problems += [f"{key}: {p}" for p in self.check_manifest(key, m)]
        except Exception as e:                         # an audit must never take the run down
            problems.append(f"audit error: {e!r}")
        self.audits.append((label, problems))

    def check_manifest(self, key: str, m: dict):
        s3, b = self.s3, self.bucket
        try:
            if s3.get_object(Bucket=b, Key=key)["Body"].read() != m["body"]:
                yield "manifest content changed"
        except botocore.exceptions.ClientError:
            yield "manifest object missing"
        st = m["state"]
        total = m["offset"] * RECORD_BYTES
        bip, tail_key, tail_len = int(st.get("bytes_in_parts", 0)), st.get("tail_key"), int(st.get("tail_len", 0))
        if bip + tail_len != total:
            yield f"bytes_in_parts {bip} + tail_len {tail_len} != {total}"
            return
        if tail_len:
            if not tail_key or not str(tail_key).startswith(f"jobs/{self.job}/"):
                yield f"tail_key {tail_key!r} not under the job prefix"
                return
            try:
                head = s3.head_object(Bucket=b, Key=tail_key)
            except botocore.exceptions.ClientError as e:
                yield f"tail {tail_key} missing ({e.response['Error'].get('Code')})"
                return
            if head["ContentLength"] != tail_len:
                yield f"tail {tail_key} is {head['ContentLength']} bytes, manifest says {tail_len}"
                return
            data = s3.get_object(Bucket=b, Key=tail_key)["Body"].read()
            if data != self.records.byte_range(bip, total):
                yield f"tail {tail_key} does not hold bytes [{bip}, {total}) of the stream"
        parts = st.get("parts") or []
        if not parts and not bip and st.get("upload_id") not in {u for _, u in self.list_uploads()}:
            yield f"upload {st.get('upload_id')} is no longer open"
        if parts or bip:
            try:
                listed = {}
                kw = {"Bucket": b, "Key": st["key"], "UploadId": st["upload_id"]}
                while True:
                    page = s3.list_parts(**kw)
                    for p in page.get("Parts", []):
                        listed[p["PartNumber"]] = p
                    if not page.get("IsTruncated"):
                        break
                    kw["PartNumberMarker"] = page["NextPartNumberMarker"]
            except botocore.exceptions.ClientError as e:
                yield f"upload {st.get('upload_id')} not listable ({e.response['Error'].get('Code')})"
                return
            size = 0
            for i, p in enumerate(sorted(parts, key=lambda p: int(p["PartNumber"]))):
                got = listed.get(int(p["PartNumber"]))
                if got is None or got["ETag"].strip('"') != str(p["ETag"]).strip('"'):
                    yield f"part {p['PartNumber']} of upload {st['upload_id']} missing or changed"
                    return
                last = i == len(parts) - 1 and not tail_len
                if got["Size"] < MIN_PART_BYTES and not last:
                    yield f"part {p['PartNumber']} is {got['Size']} bytes (< 5 MiB) and not the last part"
                size += got["Size"]
            if size != bip:
                yield f"listed parts hold {size} bytes, bytes_in_parts says {bip}"

    def final_checks(self):
        f = self.final
        retained = dict(self.manifests)
        tails = {m["state"].get("tail_key") for m in retained.values() if m["state"].get("tail_key")}
        uploads = {m["state"].get("upload_id") for m in retained.values()}
        keys = self.list_keys()
        mine = f"jobs/{self.job}/"
        meta = (mine + "chk/", mine + "savepoints/")
        f["stray_objects"] = sorted(k for k in keys if k.startswith(mine) and not k.startswith(meta) and k not in tails)
        f["foreign_objects"] = sorted(k for k in keys if not k.startswith(mine) and k not in self.other_objects)
        f["missing_tails"] = sorted(t for t in tails if t not in keys)
        f["stray_uploads"] = sorted(f"{k} {u}" for k, u in self.list_uploads() if u not in uploads)
        other = []
        for key, body in self.other_objects.items():
            try:
                if self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read() != body:
                    other.append(f"{key} changed")
            except botocore.exceptions.ClientError:
                other.append(f"{key} deleted")
        f["other_job"] = other
        hidden = []
        v = self.s3.list_object_versions(Bucket=self.bucket)
        for d in v.get("DeleteMarkers", []):
            if not d["Key"].startswith(meta):
                hidden.append(f"delete marker {d['Key']}")
        for ver in v.get("Versions", []):
            if not ver.get("IsLatest", True) and not ver["Key"].startswith(meta):
                hidden.append(f"noncurrent version {ver['Key']}")
        f["hidden_versions"] = hidden

    def publish_and_compare(self):
        """Complete the upload of the newest manifest (its parts + its tail as the last part)."""
        latest = self.manifests[self.chk_key(self.k(9))]["state"]
        parts = [{"PartNumber": int(p["PartNumber"]), "ETag": p["ETag"]} for p in latest["parts"]]
        if latest.get("tail_key"):
            tail = self.s3.get_object(Bucket=self.bucket, Key=latest["tail_key"])["Body"].read()
            n = max([p["PartNumber"] for p in parts] or [0]) + 1
            etag = self.s3.upload_part(Bucket=self.bucket, Key=latest["key"], UploadId=latest["upload_id"],
                                       PartNumber=n, Body=tail)["ETag"]
            parts.append({"PartNumber": n, "ETag": etag})
        self.s3.complete_multipart_upload(Bucket=self.bucket, Key=latest["key"], UploadId=latest["upload_id"],
                                          MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])})
        body = self.s3.get_object(Bucket=self.bucket, Key=latest["key"])["Body"].read()
        expected = self.records.span(0, 16000)
        self.final["published_ok"] = hashlib.sha256(body).digest() == hashlib.sha256(expected).digest()
        self.final["published_len"] = (len(body), len(expected))


def run_everything() -> Run:
    run = Run()
    try:
        run.schedule()
    except ProtocolError as e:
        run.errors.append(str(e))
    except Exception as e:
        run.errors.append(f"schedule aborted: {e!r}")
    for a in run.attempts:                              # no attempt outlives the schedule
        if a.proc.poll() is None:
            a.kill()
    try:
        if run.completed_schedule:
            run.final_checks()
            run.publish_and_compare()
    except Exception as e:
        run.errors.append(f"final checks aborted: {e!r}")
    with open(os.path.join(LOG_DIR, "schedule.json"), "w") as fh:
        json.dump({"bucket": run.bucket, "job": run.job, "base": run.base, "uploads": run.uploads,
                   "restores": run.restores, "cancel_checks": run.cancel_checks, "errors": run.errors, "audits": run.audits,
                   "final": run.final, "completed": run.completed_schedule}, fh, indent=1, default=str)
    return run
