"""Create the queue and tables the refresher uses (idempotent). Ops runs this per environment."""

import json
import os

from botocore.exceptions import ClientError

from . import config

QUEUE_VISIBILITY_TIMEOUT = 60


def _table(client, name):
    try:
        client.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise


def main(args):
    sqs = config.sqs()
    name = os.environ.get("REFRESH_QUEUE_NAME", "refresh-jobs")
    queue_url = sqs.create_queue(QueueName=name, Attributes={
        "VisibilityTimeout": str(QUEUE_VISIBILITY_TIMEOUT)})["QueueUrl"]
    os.environ["REFRESH_QUEUE_URL"] = queue_url
    cfg = config.load()
    client = config.dynamodb()
    for table in (cfg.unique_table, cfg.overlap_table, cfg.entity_table):
        _table(client, table)
    print(json.dumps({"queue_url": queue_url, "tables": [cfg.unique_table, cfg.overlap_table,
                                                         cfg.entity_table]}))
    return 0
