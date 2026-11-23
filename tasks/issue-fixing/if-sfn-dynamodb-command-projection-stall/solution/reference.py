"""Queue consumers for command processing."""
import json
import logging
import os
import sys
import time
from botocore.exceptions import ClientError

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

    def read(self, event, offset):
        for attempt in range(3):
            try:
                return self.ddb.get_item(TableName=self.table, Key=self.key(event, offset),
                                         ConsistentRead=True).get("Item", {})
            except ClientError:
                if attempt == 2:
                    LOG.exception("command read unavailable")
                    return {}
                time.sleep(0.1 * 2 ** attempt)

    def release(self, token, by):
        try:
            self.resume(token, by)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in ("TaskDoesNotExist", "TaskTimedOut"):
                raise
            LOG.info("callback already closed: %s", code)

    def wait(self, event):
        try:
            self.ddb.update_item(
                TableName=self.table, Key=self.key(event),
                UpdateExpression="SET taskToken = :t, executionArn = :e",
                ConditionExpression="attribute_not_exists(taskToken) OR taskToken = :t",
                ExpressionAttributeValues={":t": {"S": event["taskToken"]},
                                           ":e": {"S": event["executionArn"]}})
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            LOG.info("another execution owns %s v%s", event["entity"], event["version"])
            return
        if event["version"] > 1:
            row = self.read(event, -1)
            if row.get("status", {}).get("S") in ("finish:STARTED", "finish:FINISHED"):
                self.release(event["taskToken"], "pull")

    def finish(self, event):
        self.status(event, "finish:STARTED")
        row = self.read(event, 1)
        if "taskToken" in row:
            self.release(row["taskToken"]["S"], "push")
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
