"""Create the queue and tables the refresher uses (idempotent). Ops runs this per environment."""

import json
import os
import sys
import hashlib
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy

from botocore.exceptions import ClientError

from . import config

QUEUE_VISIBILITY_TIMEOUT = 60


def _table(client, name):
    try:
        # Existing tables are left intact, as with create_table's ResourceInUse path.
        try:
            client.describe_table(TableName=name)
            return
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        deploy(boto3.client("cloudformation"),
               "refresher-table-" + hashlib.sha256(name.encode()).hexdigest(),
               Path(__file__).resolve().parents[1] / "infra/seed.template.json",
               TableName=name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise


def main(args):
    sqs = config.sqs()
    name = os.environ.get("REFRESH_QUEUE_NAME", "refresh-jobs")
    queue_url = deploy(boto3.client("cloudformation"),
                       "refresher-queue-" + hashlib.sha256(name.encode()).hexdigest(),
                       Path(__file__).resolve().parents[1] / "infra/queue.template.json",
                       QueueName=name)["QueueUrl"]
    os.environ["REFRESH_QUEUE_URL"] = queue_url
    cfg = config.load()
    client = config.dynamodb()
    for table in (cfg.unique_table, cfg.overlap_table, cfg.entity_table):
        _table(client, table)
    print(json.dumps({"queue_url": queue_url, "tables": [cfg.unique_table, cfg.overlap_table,
                                                         cfg.entity_table]}))
    return 0
