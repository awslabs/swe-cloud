"""Reliability worker: the port of the Reliability Lambda and its SQS event source mappings.

Each worker process long-polls both work queues, one message at a time, and hands the
submission to Notify. A message is deleted only when processing returns normally; on any error
it is left on the queue and SQS delivers it again after the visibility timeout.
"""
import json
import signal

from . import config, log, render, store
from .notify import NotifyClient


class Worker:
    def __init__(self, worker_id):
        self.worker_id = worker_id
        self.notify = NotifyClient(config.NOTIFY_API_URL, config.NOTIFY_API_KEY, worker_id=worker_id)
        self.running = True

    # -- one submission -------------------------------------------------------------------
    def process(self, submission_id):
        """Deliver one submission. Returning normally acknowledges the message."""
        item = store.get_submission(submission_id)

        # Check if form data exists or was already processed.
        if item is None or not item.get("FormData") or item.get("NotifyState") in (
            store.SENT,
            store.DELIVERED,
        ):
            log.info(
                worker=self.worker_id,
                submissionId=submission_id,
                message="Submission will not be processed because it could not be found in the "
                "database or has already been processed.",
            )
            return

        response = self.notify.send_email(
            config.NOTIFY_TEMPLATE_ID,
            render.recipient(item),
            personalisation=render.personalisation(submission_id, item),
            reference=submission_id,
        )
        store.notify_processed(submission_id, response["id"])
        log.info(
            worker=self.worker_id,
            submissionId=submission_id,
            notificationId=response["id"],
            message="Successfully sent submission through GC Notify.",
        )

    # -- queue plumbing -------------------------------------------------------------------
    def handle(self, queue_url, message):
        body = json.loads(message["Body"])
        submission_id = body["submissionID"]
        try:
            self.process(submission_id)
        except Exception as error:  # leave the message; SQS redelivers it
            log.error(
                worker=self.worker_id,
                submissionId=submission_id,
                message="Failed to process submission.",
                error=repr(error),
            )
            return
        config.sqs().delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])

    def poll(self, queue_url):
        response = config.sqs().receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=config.POLL_WAIT_S,
        )
        for message in response.get("Messages", []):
            self.handle(queue_url, message)

    def run(self):
        queues = [config.submission_queue_url(), config.reprocess_queue_url()]
        log.info(worker=self.worker_id, message="worker started", queues=queues)
        while self.running:
            for queue_url in queues:
                if not self.running:
                    break
                try:
                    self.poll(queue_url)
                except Exception as error:
                    log.error(worker=self.worker_id, message="poll failed", error=repr(error))


def main(worker_id):
    worker = Worker(worker_id)

    def stop(*_):
        worker.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()
