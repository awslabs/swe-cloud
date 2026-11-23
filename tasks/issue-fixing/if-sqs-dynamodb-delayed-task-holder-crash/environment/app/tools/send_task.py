"""Publish one delayed task."""

import argparse
import json
import time

import boto3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--due-in", type=float, required=True,
                        help="seconds from now at which the task is due")
    parser.add_argument("--endpoint", default="http://localhost:4566")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    client = boto3.client("sqs", endpoint_url=args.endpoint, region_name=args.region)
    body = {
        "run_id": args.run_id,
        "task_id": args.task_id,
        "due_at": time.time() + args.due_in,
        "payload": {"note": args.task_id},
    }
    resp = client.send_message(
        QueueUrl=args.queue_url, MessageBody=json.dumps(body)
    )
    print(json.dumps({"task_id": args.task_id,
                      "message_id": resp.get("MessageId"),
                      "due_at": body["due_at"]}))


if __name__ == "__main__":
    main()
