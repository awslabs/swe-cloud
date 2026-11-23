#!/usr/bin/env python3
"""Create a dev orders queue (VisibilityTimeout 6 s, redrive maxReceiveCount 5) and its DLQ."""
import json
import hashlib
from pathlib import Path
import sys

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy

name = sys.argv[1] if len(sys.argv) > 1 else "dev-orders"
sqs = boto3.client("sqs", endpoint_url="http://localhost:4566", region_name="us-east-1")
cfn = boto3.client("cloudformation", endpoint_url=sqs.meta.endpoint_url, region_name=sqs.meta.region_name)
outputs = deploy(cfn, "seed-orders-" + hashlib.sha256(name.encode()).hexdigest()[:24],
                 Path(__file__).resolve().parents[1] / "infra/seed.template.json",
                 QueueName=name, DlqName=f"{name}-dlq", VisibilityTimeout=6, MaxReceiveCount=5)
url = outputs["QueueUrl"]
print(url)
