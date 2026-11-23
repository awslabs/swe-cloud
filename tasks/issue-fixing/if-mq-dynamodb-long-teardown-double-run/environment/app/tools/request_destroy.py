"""Request destroys the way the VM service does.

    python3 tools/request_destroy.py vm-a vm-b ...

Makes sure each VM has an ``active`` record in FLEET_TABLE (development convenience; in production
the VM service created it at launch), then publishes one persistent {"vm_id": ...} message per VM to
WORK_QUEUE on the default exchange, with publisher confirms.
"""

import json
import os
import sys
import time

import boto3
import pika


def main(vm_ids):
    if not vm_ids:
        sys.exit(__doc__)
    ddb = boto3.client("dynamodb")
    for vm_id in vm_ids:
        try:
            ddb.put_item(
                TableName=os.environ["FLEET_TABLE"],
                Item={"vm_id": {"S": vm_id}, "state": {"S": "active"}, "launched_at": {"N": str(int(time.time() * 1000))}},
                ConditionExpression="attribute_not_exists(vm_id)",
            )
        except ddb.exceptions.ConditionalCheckFailedException:
            pass
    connection = pika.BlockingConnection(pika.URLParameters(os.environ["AMQP_URL"]))
    channel = connection.channel()
    channel.confirm_delivery()
    for vm_id in vm_ids:
        channel.basic_publish(
            exchange="", routing_key=os.environ.get("WORK_QUEUE", "vm.destroy"),
            body=json.dumps({"vm_id": vm_id}).encode(),
            properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
        )
        print(f"requested destroy of {vm_id}")
    connection.close()


if __name__ == "__main__":
    main(sys.argv[1:])
