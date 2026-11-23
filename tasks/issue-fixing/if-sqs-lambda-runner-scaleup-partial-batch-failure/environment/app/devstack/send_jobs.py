#!/usr/bin/env python3
"""Queue workflow_job events on the dev build queue, as the webhook function does.

    python3 devstack/send_jobs.py org-a:3 org-b:2 [--queue ghr-dev-queued-builds] [--first-id 100]
"""

from __future__ import annotations

import argparse
import json
import os

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("owners", nargs="+", help="owner:count")
    parser.add_argument("--queue", default=os.environ.get("BUILD_QUEUE", "ghr-dev-queued-builds"))
    parser.add_argument("--first-id", type=int, default=100)
    parser.add_argument("--owner-type", default="Organization")
    args = parser.parse_args()
    sqs = boto3.client("sqs")
    url = sqs.get_queue_url(QueueName=args.queue)["QueueUrl"]
    job_id = args.first_id
    for spec in args.owners:
        owner, _, count = spec.partition(":")
        for _ in range(int(count or 1)):
            job_id += 1
            body = {
                "id": job_id,
                "eventType": "workflow_job",
                "repositoryName": "app",
                "repositoryOwner": owner,
                "installationId": 1,
                "repoOwnerType": args.owner_type,
                "labels": ["self-hosted", "linux", "x64"],
            }
            sent = sqs.send_message(QueueUrl=url, MessageBody=json.dumps(body))
            print(f"queued job {job_id} for {owner}: {sent['MessageId']}")


if __name__ == "__main__":
    main()
