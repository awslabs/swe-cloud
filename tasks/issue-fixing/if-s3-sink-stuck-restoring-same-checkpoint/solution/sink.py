#!/usr/bin/env python3
"""partsink task process: one attempt of the archiver's sink, driven by the job manager.

    python3 /app/sink.py --bucket B --job J --attempt N [--restore-from jobs/J/chk/00000007.json]

Speaks the line protocol in README.md on stdin/stdout; logs go to stderr.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import boto3
from botocore.config import Config

from partsink.bucket import Bucket
from partsink.recoverable import S3Recoverable
from partsink.writer import S3RecoverableWriter

LOG = logging.getLogger("partsink.sink")

EXIT_RESTORE_FAILED = 2
EXIT_OPEN_FAILED = 3


def s3_client():
    return boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                        config=Config(retries={"max_attempts": 5, "mode": "standard"}))


def part_key(job: str) -> str:
    return f"jobs/{job}/out/part-0"


def load_manifest(s3, bucket: str, key: str) -> dict:
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


class Sink:
    def __init__(self, s3, bucket: str, job: str, attempt: int):
        self.s3 = s3
        self.bucket_name = bucket
        self.job = job
        self.attempt = attempt
        self.bucket = Bucket(S3RecoverableWriter(s3, bucket), part_key(job))

    def start(self) -> str:
        upload_id = self.bucket.open_new_part_file()
        return f"STARTED {upload_id}"

    def restore(self, manifest_key: str) -> str:
        manifest = load_manifest(self.s3, self.bucket_name, manifest_key)
        self.bucket.restore_in_progress_file(S3Recoverable.from_dict(manifest["state"]))
        return f"RESTORED {manifest['checkpoint_id']}"

    def write(self, records) -> str:
        for record in records:
            self.bucket.write(record)
        return f"WROTE {self.bucket.offset}"

    def checkpoint(self, checkpoint_id: int) -> str:
        resumable = self.bucket.on_reception_of_checkpoint(checkpoint_id)
        return f"SNAPSHOT {checkpoint_id} {resumable.to_json()}"

    def complete(self, checkpoint_id: int) -> str:
        self.bucket.on_successful_completion_of_checkpoint(checkpoint_id)
        return f"COMPLETED {checkpoint_id}"


def reply(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bucket", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--restore-from", help="manifest key (checkpoint or savepoint) to restore from")
    args = p.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("PARTSINK_LOG", "INFO"),
                        format=f"%(asctime)s a{args.attempt} %(name)s %(levelname)s %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)

    sink = Sink(s3_client(), args.bucket, args.job, args.attempt)
    try:
        reply(sink.restore(args.restore_from) if args.restore_from else sink.start())
    except Exception:
        LOG.exception("could not initialise the in-progress part file")
        return EXIT_RESTORE_FAILED

    stdin = sys.stdin.buffer
    line = stdin.readline().decode().strip()
    if line != "OPEN ok":
        LOG.error("open barrier: %r, exiting", line)
        return EXIT_OPEN_FAILED
    reply("OPENED")

    while True:
        raw = stdin.readline()
        if not raw:
            return 0
        cmd, _, arg = raw.decode().strip().partition(" ")
        if cmd == "WRITE":
            reply(sink.write(stdin.readline() for _ in range(int(arg))))
        elif cmd == "CHECKPOINT":
            reply(sink.checkpoint(int(arg)))
        elif cmd == "COMPLETE":
            reply(sink.complete(int(arg)))
        elif cmd == "END":
            sink.bucket.on_end_of_job()
            return 0
        else:
            LOG.error("unknown command %r", raw[:80])
            return 1


if __name__ == "__main__":
    sys.exit(main())
