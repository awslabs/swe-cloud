#!/usr/bin/env python3
"""Local event source mapping: poll the build queue and invoke the scale-up function, as Lambda does.

    python3 devstack/esm.py [--queue ghr-dev-queued-builds]

Settings come from deploy/scale-up.json: batch size, batching window, function timeout. Each
invocation runs the function in its own process through control_plane.runtime, with the
function's environment from the deploy file (endpoints from this process's environment).

With ReportBatchItemFailures the response decides each record:
- every record not listed in batchItemFailures is deleted;
- every listed record stays on the queue and is received again once its visibility timeout ends;
- an empty or null list is a complete success (everything is deleted);
- an exception, a timeout, invalid JSON, or an itemIdentifier that is empty or not a messageId of
  the batch is a complete failure: nothing is deleted and the whole batch comes back.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3

APP = Path(__file__).resolve().parent.parent
DEPLOY = json.loads((APP / "deploy" / "scale-up.json").read_text())


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} esm {message}", flush=True)


def lambda_record(message: dict, queue_arn: str) -> dict:
    return {
        "messageId": message["MessageId"],
        "receiptHandle": message["ReceiptHandle"],
        "body": message["Body"],
        "attributes": message.get("Attributes", {}),
        "messageAttributes": message.get("MessageAttributes", {}),
        "md5OfBody": message.get("MD5OfBody"),
        "eventSource": "aws:sqs",
        "eventSourceARN": queue_arn,
        "awsRegion": "us-east-1",
    }


def invoke(event: dict) -> tuple[str, list[str] | None]:
    """Run one invocation. Returns (outcome, listed message ids or None for a complete failure)."""
    timeout_s = DEPLOY["lambda"]["timeout_seconds"]
    function_env = {**os.environ, **DEPLOY["environment"], "PYTHONPATH": str(APP)}
    function_env["LAMBDA_DEADLINE_MS"] = str(int((time.time() + timeout_s) * 1000))
    with tempfile.TemporaryDirectory(prefix="scale-up-") as sandbox:
        function_env.update(HOME=sandbox, TMPDIR=sandbox)
        try:
            done = subprocess.run(
                [sys.executable, "-m", "control_plane.runtime", DEPLOY["lambda"]["handler"]],
                input=json.dumps(event), capture_output=True, text=True, timeout=timeout_s, cwd=sandbox, env=function_env,
            )
        except subprocess.TimeoutExpired as expired:
            sys.stderr.write((expired.stderr or b"").decode() if isinstance(expired.stderr, bytes) else (expired.stderr or ""))
            return "timeout", None
    sys.stderr.write(done.stderr)
    if done.returncode != 0:
        return f"function error (exit {done.returncode})", None
    try:
        response = json.loads(done.stdout)
        failures = response.get("batchItemFailures") or [] if isinstance(response, dict) else None
        ids = [item["itemIdentifier"] for item in failures]
    except (ValueError, TypeError, KeyError, AttributeError):
        return "invalid response", None
    known = {record["messageId"] for record in event["Records"]}
    if any(not isinstance(i, str) or i not in known for i in ids):
        return "invalid itemIdentifier", None
    return "ok", ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", default=os.environ.get("BUILD_QUEUE", "ghr-dev-queued-builds"))
    args = parser.parse_args()
    sqs = boto3.client("sqs")
    url = sqs.get_queue_url(QueueName=args.queue)["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    batch_size = DEPLOY["event_source_mapping"]["batch_size"]
    window_s = DEPLOY["event_source_mapping"]["maximum_batching_window_in_seconds"]
    log(f"polling {args.queue} (batch {batch_size}, window {window_s}s)")
    while True:
        batch = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=batch_size, WaitTimeSeconds=5, AttributeNames=["All"], MessageAttributeNames=["All"]).get("Messages", [])
        if not batch:
            continue
        window_end = time.monotonic() + window_s
        while len(batch) < batch_size and time.monotonic() < window_end:
            batch += sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=batch_size - len(batch), WaitTimeSeconds=1, AttributeNames=["All"], MessageAttributeNames=["All"]).get("Messages", [])
        event = {"Records": [lambda_record(message, arn) for message in batch]}
        outcome, listed = invoke(event)
        if listed is None:
            log(f"invocation of {len(batch)} record(s): {outcome}; whole batch will be retried")
            continue
        delete = [m for m in batch if m["MessageId"] not in listed]
        for start in range(0, len(delete), 10):
            sqs.delete_message_batch(QueueUrl=url, Entries=[{"Id": str(n), "ReceiptHandle": m["ReceiptHandle"]} for n, m in enumerate(delete[start:start + 10])])
        log(f"invocation of {len(batch)} record(s): deleted {len(delete)}, returned {len(listed)} for retry")


if __name__ == "__main__":
    sys.exit(main())
