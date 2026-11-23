"""Put an EC2 Spot Instance Interruption Warning on a queue, shaped as EventBridge delivers it.

    python3 -m devcluster.notice --queue-url "$QUEUE_URL" --instance-id i-0f1e2d3c4b5a69780
"""

import argparse
import json
import uuid
from datetime import datetime, timezone

import boto3


def envelope(instance_id, account="000000000000", region="us-east-1"):
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "EC2 Spot Instance Interruption Warning",
        "source": "aws.ec2",
        "account": account,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": region,
        "resources": [f"arn:aws:ec2:{region}b:instance/{instance_id}"],
        "detail": {"instance-id": instance_id, "instance-action": "terminate"},
    }


def main():
    parser = argparse.ArgumentParser(prog="python3 -m devcluster.notice")
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--instance-id", required=True, action="append")
    args = parser.parse_args()
    sqs = boto3.client("sqs")
    for instance_id in args.instance_id:
        result = sqs.send_message(QueueUrl=args.queue_url,
                                  MessageBody=json.dumps(envelope(instance_id)))
        print(f"{instance_id} {result['MessageId']}")


if __name__ == "__main__":
    main()
