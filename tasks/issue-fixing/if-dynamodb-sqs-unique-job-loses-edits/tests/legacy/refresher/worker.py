"""Long-running queue worker."""

import signal
import sys
import time

from . import config
from .handler import CallQueuedHandler
from .queue import SqsJob


def log(worker_id, message):
    print(f"{time.strftime('%H:%M:%S')} worker={worker_id} {message}", file=sys.stderr,
          flush=True)


class Worker:
    def __init__(self, worker_id, cfg=None):
        self.worker_id = worker_id
        self.cfg = cfg or config.load()
        self.sqs = config.sqs()
        self.handler = CallQueuedHandler(self.cfg, config.dynamodb())
        self.stopping = False

    def stop(self, *_):
        self.stopping = True

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log(self.worker_id, f"polling {self.cfg.queue_url}")
        while not self.stopping:
            received = self.sqs.receive_message(
                QueueUrl=self.cfg.queue_url, MaxNumberOfMessages=1,
                WaitTimeSeconds=self.cfg.receive_wait, AttributeNames=["All"])
            for message in received.get("Messages", []):
                self.process(SqsJob(self.sqs, self.cfg.queue_url, message))
        log(self.worker_id, "stopped")

    def process(self, job):
        log(self.worker_id, f"received message_id={job.message_id} attempt={job.attempts()}")
        if job.attempts() > self.cfg.max_tries:
            job.fail("max attempts exceeded")
            return
        try:
            self.handler.call(job)
            if job.released:
                log(self.worker_id, f"released message_id={job.message_id}")
            else:
                log(self.worker_id, f"done message_id={job.message_id}")
        except Exception as error:  # the job failed; retry it unless it has used its tries
            log(self.worker_id, f"error message_id={job.message_id}: {error}")
            if job.deleted or job.released:
                return
            if job.attempts() >= self.cfg.max_tries:
                job.fail(error)
            else:
                job.release(self.cfg.backoff)


def main(args):
    Worker(args.id).run()
    return 0
