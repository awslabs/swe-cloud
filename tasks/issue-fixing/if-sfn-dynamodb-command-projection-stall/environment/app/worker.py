"""Queue consumers for command processing."""
import json
import logging
import os
import sys

import boto3
from botocore.config import Config

LOG = logging.getLogger("commands")


def client(service):
    return boto3.client(service, endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                        region_name=os.environ.get("AWS_REGION", "us-east-1"),
                        aws_access_key_id="test", aws_secret_access_key="test",
                        config=Config(retries={"max_attempts": 0}, read_timeout=60))


class Worker:
    def __init__(self):
        self.ddb = client("dynamodb")
        self.sfn = client("stepfunctions")
        self.sqs = client("sqs")
        self.table = os.environ.get("COMMANDS_TABLE", "commands")

    @staticmethod
    def key(event, offset=0):
        return {"pk": {"S": event["entity"]}, "sk": {"N": str(event["version"] + offset)}}

    def status(self, event, value):
        self.ddb.update_item(TableName=self.table, Key=self.key(event),
                             UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                             ExpressionAttributeValues={":s": {"S": value}})
        LOG.info("%s v%s %s", event["entity"], event["version"], value)

    def resume(self, token, by):
        self.sfn.send_task_success(taskToken=token, output=json.dumps({"result": "resumed", "by": by}))

    def wait(self, event):
        self.status(event, "wait_prev_command:STARTED")
        self.ddb.update_item(TableName=self.table, Key=self.key(event),
                             UpdateExpression="SET taskToken = :t, executionArn = :e",
                             ExpressionAttributeValues={":t": {"S": event["taskToken"]},
                                                        ":e": {"S": event["executionArn"]}})
        self.status(event, "wait_prev_command:FINISHED")

    def finish(self, event):
        self.status(event, "finish:STARTED")
        row = self.ddb.get_item(TableName=self.table, Key=self.key(event, 1)).get("Item", {})
        if "taskToken" in row:
            try:
                self.resume(row["taskToken"]["S"], "push")
            except Exception:
                LOG.warning("could not resume next command", exc_info=True)
        self.status(event, "finish:FINISHED")
        self.sfn.send_task_success(taskToken=event["taskToken"], output='{"result":"ok"}')

    def run(self, mode):
        name = "wait-prev" if mode == "wait-worker" else "finish"
        setting = "WAIT_QUEUE_URL" if mode == "wait-worker" else "FINISH_QUEUE_URL"
        queue = os.environ.get(setting) or self.sqs.get_queue_url(QueueName=name)["QueueUrl"]
        handler = self.wait if mode == "wait-worker" else self.finish
        while True:
            messages = self.sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=1,
                                                WaitTimeSeconds=1).get("Messages", [])
            for message in messages:
                event = json.loads(message["Body"])
                try:
                    handler(event)
                except Exception:
                    self.status(event, ("wait_prev_command" if mode == "wait-worker" else "finish") + ":FAILED")
                    LOG.exception("step failed")
                    raise
                self.sqs.delete_message(QueueUrl=queue, ReceiptHandle=message["ReceiptHandle"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in ("wait-worker", "finish-worker"):
        raise SystemExit("usage: solution wait-worker|finish-worker")
    Worker().run(sys.argv[1])
