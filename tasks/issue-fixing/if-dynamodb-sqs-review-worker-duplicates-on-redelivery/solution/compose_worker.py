"""compose-review worker: consumes upload-part messages and runs ComposeReview.

    python3 /app/compose_worker.py --worker-id <id>

Message body: {"req_id": ..., "part": ..., "value": ..., "movie_id": ...}

Every step of an upload can be repeated: a message whose worker died is simply
processed again after redelivery, and the handler finishes whatever its earlier
execution left undone. The reply is committed with the upload's increment.
"""
import argparse
import json
import logging
import sys

import boto3

from compose_review import ComposeReview
from settings import Settings

RECEIVE_WAIT_S = 1

log = logging.getLogger("compose_worker")


class Worker:
    def __init__(self, worker_id, settings):
        self.worker_id = worker_id
        self.s = settings
        self.sqs = boto3.client("sqs")
        self.ddb = boto3.client("dynamodb")
        self.service = ComposeReview(self.ddb, self.sqs, settings)

    def start(self):
        log.info("worker %s polling %s", self.worker_id, self.s.queue_url)
        while True:
            out = self.sqs.receive_message(
                QueueUrl=self.s.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=RECEIVE_WAIT_S,
            )
            for message in out.get("Messages", []):
                try:
                    self.handle(message)
                except Exception:
                    log.exception("upload failed; leaving the message for redelivery")

    def handle(self, message):
        body = json.loads(message["Body"])
        req_id, part, value = body["req_id"], body["part"], body["value"]
        result = self.service.upload(req_id, part, value)
        self.sqs.delete_message(QueueUrl=self.s.queue_url, ReceiptHandle=message["ReceiptHandle"])
        log.info("%s %s -> %s", req_id, part, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description="compose-review worker")
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format=f"%(asctime)s {args.worker_id} %(levelname)s %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    Worker(args.worker_id, Settings.from_env()).start()


if __name__ == "__main__":
    main()
