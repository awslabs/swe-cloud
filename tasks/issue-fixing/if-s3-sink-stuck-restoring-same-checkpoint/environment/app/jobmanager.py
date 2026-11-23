#!/usr/bin/env python3
"""Local stand-in for the archiver's job manager.

Runs one job end to end against the S3 endpoint: launches the sink (python3 /app/sink.py) as a
fresh process per attempt, streams the source's records into it, triggers checkpoints, writes and
retires checkpoint manifests, takes savepoints on request, and restarts the sink from the latest
retained checkpoint when an attempt fails. Same protocol, layout and retention as production;
see README.md.

    python3 /app/jobmanager.py --bucket archive-dev --job order-events
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

import boto3

LOG = logging.getLogger("jobmanager")

RECORD_BYTES = 1024
RETAINED_CHECKPOINTS = 2            # execution.checkpointing.num-retained
MAX_SNAPSHOT_BYTES = 4096           # checkpoint metadata limit per operator
REPLY_TIMEOUT_S = 30
_PRINTABLE = bytes(33 + (b % 94) for b in range(256))


def record(seed: str, index: int) -> bytes:
    """The source's record at a position: 1023 printable bytes and a newline."""
    return hashlib.shake_256(f"{seed}:{index}".encode()).digest(RECORD_BYTES - 1).translate(_PRINTABLE) + b"\n"


def chk_key(job: str, checkpoint_id: int) -> str:
    return f"jobs/{job}/chk/{checkpoint_id:08d}.json"


class AttemptFailed(Exception):
    pass


class SinkProcess:
    def __init__(self, bucket: str, job: str, attempt: int, restore_from: str | None):
        cmd = [sys.executable, "/app/sink.py", "--bucket", bucket, "--job", job, "--attempt", str(attempt)]
        if restore_from:
            cmd += ["--restore-from", restore_from]
        self.workdir = tempfile.mkdtemp(prefix=f"attempt-{attempt}-")
        env = {**os.environ, "PYTHONPATH": "/app", "TMPDIR": self.workdir, "HOME": self.workdir}
        self.proc = subprocess.Popen(cmd, cwd=self.workdir, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=None)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for raw in self.proc.stdout:
            self.lines.put(raw.decode().rstrip("\n"))
        self.lines.put(None)

    def send(self, data: bytes) -> None:
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise AttemptFailed(f"sink stdin closed: {e}")

    def expect(self, word: str) -> str:
        try:
            line = self.lines.get(timeout=REPLY_TIMEOUT_S)
        except queue.Empty:
            raise AttemptFailed(f"no {word} within {REPLY_TIMEOUT_S}s")
        if line is None:
            raise AttemptFailed(f"sink exited with {self.proc.wait()} while waiting for {word}")
        head, _, rest = line.partition(" ")
        if head != word:
            raise AttemptFailed(f"expected {word}, got {line[:200]!r}")
        return rest

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


class JobManager:
    def __init__(self, s3, args):
        self.s3 = s3
        self.args = args
        self.bucket = args.bucket
        self.job = args.job
        self.next_checkpoint_id = 1 + max(self.list_checkpoint_ids() or [0])

    # -- checkpoint store -------------------------------------------------------------------

    def list_checkpoint_ids(self):
        ids = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"jobs/{self.job}/chk/"):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if name.endswith(".json") and name[:-5].isdigit():
                    ids.append(int(name[:-5]))
        return sorted(ids)

    def complete_checkpoint(self, checkpoint_id: int, attempt: int, state: dict) -> None:
        manifest = {"job": self.job, "checkpoint_id": checkpoint_id, "attempt": attempt, "state": state}
        self.s3.put_object(Bucket=self.bucket, Key=chk_key(self.job, checkpoint_id),
                           Body=json.dumps(manifest).encode(), ContentType="application/json")
        for old in self.list_checkpoint_ids()[:-RETAINED_CHECKPOINTS]:
            self.s3.delete_object(Bucket=self.bucket, Key=chk_key(self.job, old))
            LOG.info("checkpoint %d subsumed", old)

    def take_savepoint(self, checkpoint_id: int) -> str:
        manifest = json.loads(self.s3.get_object(Bucket=self.bucket, Key=chk_key(self.job, checkpoint_id))["Body"].read())
        manifest["savepoint"] = True
        key = f"jobs/{self.job}/savepoints/sp-{checkpoint_id:08d}-{os.urandom(3).hex()}.json"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(manifest).encode(),
                           ContentType="application/json")
        LOG.info("savepoint %s", key)
        return key

    def manifest_offset(self, key: str) -> int:
        return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())["state"]["offset"]

    # -- one attempt ------------------------------------------------------------------------

    def run_attempt(self, attempt: int, restore_from: str | None) -> None:
        a = self.args
        sink = SinkProcess(self.bucket, self.job, attempt, restore_from)
        try:
            if restore_from:
                sink.expect("RESTORED")
                offset = self.manifest_offset(restore_from)
            else:
                LOG.info("attempt %d: part file upload %s", attempt, sink.expect("STARTED"))
                offset = 0
            sink.send(b"OPEN ok\n")       # every other operator of the job opened
            sink.expect("OPENED")
            while offset < a.records:
                n = min(a.batch, a.records - offset, a.checkpoint_every - offset % a.checkpoint_every)
                sink.send(f"WRITE {n}\n".encode() + b"".join(record(a.seed, i) for i in range(offset, offset + n)))
                offset += n
                if int(sink.expect("WROTE")) != offset:
                    raise AttemptFailed("sink lost records")
                if offset % a.checkpoint_every == 0 or offset == a.records:
                    self.checkpoint(sink, attempt, offset)
            sink.send(b"END\n")
            if sink.proc.wait(timeout=REPLY_TIMEOUT_S) != 0:
                raise AttemptFailed(f"sink exited with {sink.proc.returncode}")
        finally:
            sink.kill()

    def checkpoint(self, sink: SinkProcess, attempt: int, offset: int) -> None:
        cid = self.next_checkpoint_id
        self.next_checkpoint_id += 1
        sink.send(f"CHECKPOINT {cid}\n".encode())
        rest = sink.expect("SNAPSHOT")
        if len(rest.encode()) > MAX_SNAPSHOT_BYTES:
            raise AttemptFailed(f"checkpoint {cid} declined: snapshot is {len(rest)} bytes")
        sid, _, payload = rest.partition(" ")
        state = json.loads(payload)
        if int(sid) != cid or state.get("offset") != offset:
            raise AttemptFailed(f"checkpoint {cid} declined: snapshot does not match")
        self.complete_checkpoint(cid, attempt, state)
        sink.send(f"COMPLETE {cid}\n".encode())
        if int(sink.expect("COMPLETED")) != cid:
            raise AttemptFailed("unexpected COMPLETED")
        LOG.info("checkpoint %d complete at record %d", cid, offset)
        if self.args.savepoint_after == cid:
            self.take_savepoint(cid)

    # -- restart strategy -------------------------------------------------------------------

    def run(self) -> int:
        restore_from = self.args.restore_from
        for attempt in range(self.args.max_restarts + 1):
            try:
                self.run_attempt(attempt, restore_from)
                LOG.info("job finished after %d attempt(s)", attempt + 1)
                return 0
            except AttemptFailed as e:
                LOG.warning("attempt %d failed: %s", attempt, e)
            ids = self.list_checkpoint_ids()
            restore_from = chk_key(self.job, ids[-1]) if ids else self.args.restore_from
            time.sleep(self.args.restart_delay)
        LOG.error("giving up after %d attempts", self.args.max_restarts + 1)
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bucket", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--records", type=int, default=16000)
    p.add_argument("--checkpoint-every", type=int, default=2000, help="records between checkpoints")
    p.add_argument("--batch", type=int, default=1000, help="records per WRITE")
    p.add_argument("--seed", default="dev", help="source seed (the records are a function of it)")
    p.add_argument("--savepoint-after", type=int, help="take a savepoint of this checkpoint id once complete")
    p.add_argument("--restore-from", help="start the job from this manifest (checkpoint or savepoint)")
    p.add_argument("--max-restarts", type=int, default=3)
    p.add_argument("--restart-delay", type=float, default=1.0)
    args = p.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s jobmanager %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)

    s3 = boto3.client("s3")
    try:
        s3.head_bucket(Bucket=args.bucket)
    except Exception:
        s3.create_bucket(Bucket=args.bucket)
    return JobManager(s3, args).run()


if __name__ == "__main__":
    sys.exit(main())
