"""Receives the messages of one poll.

SQS returns at most 10 messages per ``ReceiveMessage``. To fetch more per poll, the task sends one
request per 10 messages concurrently (bounded by ``concurrent_request_limit``) and returns the union
once every request has completed.
"""

import concurrent.futures
import logging
import math

LOG = logging.getLogger(__name__)

MAX_NUMBER_OF_MESSAGES_PER_REQUEST = 10


class PollingError(Exception):
    pass


class Poller:

    def __init__(self, client, config):
        self.client = client
        self.queue_url = config.queue_url
        self.max_messages_per_poll = max(1, config.max_messages_per_poll)
        self.visibility_timeout = config.visibility_timeout
        self.wait_time_seconds = config.wait_time_seconds
        self.number_of_requests_per_poll = math.ceil(
            self.max_messages_per_poll / MAX_NUMBER_OF_MESSAGES_PER_REQUEST)
        self.request_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.number_of_requests_per_poll, max(1, config.concurrent_request_limit)),
            thread_name_prefix="receive")
        self._closed = False

    def close(self):
        self._closed = True
        self.request_executor.shutdown(wait=False, cancel_futures=True)

    def call(self):
        if self._closed:
            return []
        errors = []
        if self.number_of_requests_per_poll == 1:
            messages = self._poll(self.max_messages_per_poll, errors)
        else:
            futures = []
            remaining = self.max_messages_per_poll
            while remaining > 0:
                number_of_messages = min(remaining, MAX_NUMBER_OF_MESSAGES_PER_REQUEST)
                futures.append(self.request_executor.submit(self._poll, number_of_messages, errors))
                remaining -= MAX_NUMBER_OF_MESSAGES_PER_REQUEST
            messages = []
            for future in futures:
                messages.extend(future.result())
        if errors and len(errors) == self.number_of_requests_per_poll:
            raise PollingError(f"all {len(errors)} receive requests of the poll failed: {errors[0]}")
        LOG.debug("received %d messages in %d requests", len(messages), self.number_of_requests_per_poll)
        return messages

    def _poll(self, max_number_of_messages, errors):
        try:
            response = self.client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=max_number_of_messages,
                VisibilityTimeout=self.visibility_timeout,
                WaitTimeSeconds=self.wait_time_seconds,
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
            )
        except Exception as error:
            LOG.error("error while polling: %s", error)
            errors.append(error)
            return []
        return response.get("Messages", [])
