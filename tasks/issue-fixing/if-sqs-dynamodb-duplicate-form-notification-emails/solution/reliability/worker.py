"""Reliability worker: the port of the Reliability Lambda and its SQS event source mappings.

Each worker process long-polls both work queues, one message at a time, and hands the
submission to Notify. A message is deleted only once the submission is durably done; otherwise
it stays on the queue (or is deferred) and SQS delivers it again.

Per attempt, the ReliabilityQueue item is the arbiter: a worker sends only while it holds an
unexpired claim for that attempt. A claim whose holder vanished is taken over once its lease
expires, and the new holder first asks Notify whether the attempt's email already exists (the
old holder may have died after Notify accepted it) before sending.

An item this version has never touched (no Attempt) may be one the previous version was in the
middle of when it was replaced: its emails carry the bare SubmissionID as reference, and its
PENDING mark may be stale (its callback reset the item for any failure receipt, current or not).
Notify's newest email for that reference is the one that stands; its status decides. The
previous version's workers were stopped when this process started, possibly mid-request, so that
lookup is trusted only once Notify's 2 s acceptance bound has passed since this process started.
Pointers the previous version dead-lettered are drained from the DLQ the same way.
"""
import json
import math
import signal
import time

from . import config, log, render, store
from .notify import NotifyClient

CLAIM_LEASE_S = 8.0        # > handler budget + slow-path headroom, < the 12 s visibility timeout
SEND_MARGIN_S = 3.0        # do not start a send with less lease than this left: Notify accepts a
                           # request within 2 s of it being sent or never (docs/notify-api.md), so a
                           # takeover that looks up after the lease expires sees every send we start
DEPLOY_SETTLE_S = 2.5      # > Notify's 2 s acceptance bound, counted from this process's start
ACK, RETRY = "ack", "retry"
RESEND_STATUSES = ("temporary-failure", "technical-failure")


def reference_for(submission_id, attempt):
    return f"{submission_id}:{attempt}"


class Worker:
    def __init__(self, worker_id):
        self.worker_id = worker_id
        self.notify = NotifyClient(config.NOTIFY_API_URL, config.NOTIFY_API_KEY, worker_id=worker_id)
        self.running = True
        self.started = time.monotonic()

    # -- one submission -------------------------------------------------------------------
    def take_over_previous(self, submission_id, item):
        """True when the previous version's standing email was adopted as this submission's."""
        emails = self.notify.get_notifications(reference=submission_id)
        if not emails:
            return False
        latest = emails[0]  # newest first: the email Notify accepted last
        status = latest.get("status")
        if status in RESEND_STATUSES:
            # It failed and nothing was accepted after it: one replacement, as attempt 2.
            store.reopen_previous(submission_id, latest["id"])
            return False
        state = {"delivered": store.DELIVERED, "permanent-failure": store.FAILED}.get(status, store.SENT)
        if not store.adopt_previous(submission_id, latest["id"], state):
            return False
        log.info(worker=self.worker_id, submissionId=submission_id, notificationId=latest["id"],
                 message="adopted the previous version's send")
        return True

    def process(self, submission_id):
        """Returns ACK, RETRY, or a number of seconds to defer the message by."""
        item = store.get_submission(submission_id)
        if (item is not None and item.get("FormData") and "Attempt" not in item
                and item.get("NotifyState", store.PENDING) == store.PENDING):
            settling = self.started + DEPLOY_SETTLE_S - time.monotonic()
            if settling > 0:
                # A worker of the previous version may have sent this one just before it was
                # stopped; Notify lists such a send only up to 2 s later.
                return settling
            if self.take_over_previous(submission_id, item):
                return ACK
        for _ in range(3):
            item = store.get_submission(submission_id)
            if item is None or not item.get("FormData"):
                return ACK
            state = item.get("NotifyState", store.PENDING)
            if state in store.DONE:
                return ACK
            if state == store.CLAIMED:
                remaining = float(item.get("LeaseExpiresAt", 0)) / 1000 - time.time()
                if remaining > 0:
                    return remaining + 0.5
            lease_until = time.time() + CLAIM_LEASE_S
            if store.claim(submission_id, item, self.worker_id, int(lease_until * 1000)):
                break
        else:
            return 1.0

        attempt = store.attempt_of(item)
        reference = reference_for(submission_id, attempt)
        notification_id = None
        if state == store.CLAIMED:
            # Taking over: the previous holder may have reached Notify before it died.
            existing = self.notify.get_notifications(reference=reference)
            if existing:
                notification_id = existing[0]["id"]
                log.info(worker=self.worker_id, submissionId=submission_id,
                         notificationId=notification_id, message="adopted earlier send")
        if notification_id is None:
            if time.time() > lease_until - SEND_MARGIN_S:
                return RETRY
            response = self.notify.send_email(
                config.NOTIFY_TEMPLATE_ID,
                render.recipient(item),
                personalisation=render.personalisation(submission_id, item),
                reference=reference,
            )
            notification_id = response["id"]

        if not store.mark_sent(submission_id, attempt, self.worker_id, notification_id):
            current = store.get_submission(submission_id) or {}
            if current.get("NotificationId") != notification_id:
                # Someone took the claim over; it reconciles through Notify by reference.
                return RETRY
        log.info(worker=self.worker_id, submissionId=submission_id, notificationId=notification_id,
                 message="Successfully sent submission through GC Notify.")
        return ACK

    # -- queue plumbing -------------------------------------------------------------------
    def handle(self, queue_url, message):
        body = json.loads(message["Body"])
        submission_id = body["submissionID"]
        try:
            outcome = self.process(submission_id)
        except Exception as error:  # leave the message; SQS redelivers it
            log.error(worker=self.worker_id, submissionId=submission_id,
                      message="Failed to process submission.", error=repr(error))
            return
        try:
            if outcome == ACK:
                config.sqs().delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
            elif outcome != RETRY:
                config.sqs().change_message_visibility(
                    QueueUrl=queue_url,
                    ReceiptHandle=message["ReceiptHandle"],
                    VisibilityTimeout=max(1, min(43200, math.ceil(outcome))),
                )
        except Exception as error:
            log.warn(worker=self.worker_id, submissionId=submission_id,
                     message="could not update message", error=repr(error))

    def poll(self, queue_url):
        response = config.sqs().receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=config.POLL_WAIT_S,
        )
        for message in response.get("Messages", []):
            self.handle(queue_url, message)

    def dead_letter_queues(self, queues):
        """The DLQs infrastructure attached to the work queues (from their redrive policy)."""
        found = []
        for url in queues:
            attributes = config.sqs().get_queue_attributes(
                QueueUrl=url, AttributeNames=["RedrivePolicy"]).get("Attributes", {})
            arn = json.loads(attributes.get("RedrivePolicy") or "{}").get("deadLetterTargetArn")
            if arn:
                dlq = config.sqs().get_queue_url(QueueName=arn.rsplit(":", 1)[-1])["QueueUrl"]
                if dlq not in found and dlq not in queues:
                    found.append(dlq)
        return found

    def run(self):
        queues = [config.submission_queue_url(), config.reprocess_queue_url()]
        try:
            queues += self.dead_letter_queues(queues)
        except Exception as error:
            log.error(worker=self.worker_id, message="could not find the DLQ", error=repr(error))
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
