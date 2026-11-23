"""Managed downstream adapter. Applying an effect is not idempotent."""
import json
import os
from worker import Worker


def main():
    w = Worker()
    queue = os.environ.get("SYNC_QUEUE_URL") or w.sqs.get_queue_url(QueueName="sync")["QueueUrl"]
    while True:
        for msg in w.sqs.receive_message(QueueUrl=queue, WaitTimeSeconds=1).get("Messages", []):
            event = json.loads(msg["Body"])
            w.status(event, "sync_data:STARTED")
            pred = "none"
            if event["version"] > 1:
                row = w.ddb.get_item(TableName=w.table, Key=w.key(event, -1), ConsistentRead=True).get("Item", {})
                pred = row.get("status", {}).get("S", "missing")
            w.ddb.put_item(TableName="effects", Item={
                "pk": {"S": event["entity"]}, "sk": {"S": str(event["version"]) + "#" + event["executionArn"]},
                "version": {"N": str(event["version"])}, "executionArn": {"S": event["executionArn"]},
                "predStatusAtSync": {"S": pred}})
            w.ddb.put_item(TableName="projection", Item={"pk": {"S": event["entity"]}, "version": {"N": str(event["version"])}})
            w.status(event, "sync_data:FINISHED")
            w.sfn.send_task_success(taskToken=event["taskToken"], output='{"result":"ok"}')
            w.sqs.delete_message(QueueUrl=queue, ReceiptHandle=msg["ReceiptHandle"])


if __name__ == "__main__":
    main()
