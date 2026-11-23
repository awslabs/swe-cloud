"""Create an empty source/checkpoint table pair for local operation."""
import argparse
import hashlib
import os
from pathlib import Path
import sys
import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-table", default="orders")
parser.add_argument("--checkpoint-table", default="cdc_checkpoints")
parser.add_argument("--endpoint-url", default=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"))
args = parser.parse_args()
db = boto3.client("dynamodb", endpoint_url=args.endpoint_url, region_name="us-east-1",
                  aws_access_key_id="test", aws_secret_access_key="test")
cfn = boto3.client("cloudformation", endpoint_url=args.endpoint_url, region_name="us-east-1",
                   aws_access_key_id="test", aws_secret_access_key="test")
seed_dir = Path(__file__).resolve().parent / "infra"
for name, template, kind in [
    (args.source_table, "seed.template.json", "source"),
    (args.checkpoint_table, "checkpoint.template.json", "checkpoint"),
]:
    try:
        db.describe_table(TableName=name)
    except db.exceptions.ResourceNotFoundException:
        stack_name = "provision-" + kind + "-" + hashlib.sha256(name.encode()).hexdigest()[:32]
        deploy(cfn, stack_name, seed_dir / template, TableName=name)
    db.get_waiter("table_exists").wait(TableName=name)
    print(name)
