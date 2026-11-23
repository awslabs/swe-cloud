#!/usr/bin/env python3
"""Ordered-destination delivery service.

Reads a run configuration, takes events for one destination off the pending
stream, delivers them to the destination receiver over HTTP and reports every
step to the platform control plane. Started by the delivery platform as:

    python3 dispatcher.py --config <run-config.json>

See docs/service.md for the service contract and the configuration reference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

CONTINUE = "CONTINUE"
RELOAD = "RELOAD"

DEFAULT_CONTROL_TIMEOUT_SECONDS = 60.0
# A receive's messages are invisible until its visibility expires — this
# process's or a stopped predecessor's — so a stream that has come back empty
# is only believed once that window, and a little slack, has passed.
VISIBILITY_SLACK_SECONDS = 1.5


def log(message: str) -> None:
    sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stderr.flush()


class RunConfig:
    """One run of the service against one destination and one pending stream."""

    KEYS = (
        "run_id",
        "phase",
        "endpoint_id",
        "queue_url",
        "receiver_url",
        "control_url",
        "delivery_batch_size",
        "http_timeout_seconds",
        "poll_wait_seconds",
        "retry_backoff_seconds",
        "visibility_seconds",
        "barrier_seconds",
    )

    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        for key in self.KEYS:
            if key not in raw:
                raise SystemExit("config is missing %r" % key)
        self.run_id = raw["run_id"]
        self.phase = raw["phase"]
        self.endpoint_id = raw["endpoint_id"]
        self.queue_url = raw["queue_url"]
        self.receiver_url = raw["receiver_url"].rstrip("/")
        self.control_url = raw["control_url"].rstrip("/")
        self.delivery_batch_size = int(raw["delivery_batch_size"])
        self.http_timeout_seconds = float(raw["http_timeout_seconds"])
        self.poll_wait_seconds = int(raw["poll_wait_seconds"])
        self.retry_backoff_seconds = float(raw["retry_backoff_seconds"])
        self.visibility_seconds = int(raw["visibility_seconds"])
        self.barrier_seconds = float(raw["barrier_seconds"])
        self.aws = raw.get(
            "aws",
            {
                "endpoint_url": os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
                "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "test"),
                "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
            },
        )


class ControlPlane:
    """Talks to the platform control plane.

    The control plane may hold a call for a long stretch before answering, so
    every call here uses a generous read timeout. Answers are actions: CONTINUE
    means carry on, RELOAD means drop whatever this process is holding and take
    the stream from the top again.
    """

    def __init__(self, base_url: str, timeout: float = DEFAULT_CONTROL_TIMEOUT_SECONDS):
        self.base_url = base_url
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None, timeout: float | None = None):
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            body = response.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}

    def announce_ready(self, ident: dict) -> None:
        deadline = time.time() + 30.0
        while True:
            try:
                self._call("POST", "/ready", ident)
                return
            except (urllib.error.URLError, OSError):
                if time.time() > deadline:
                    raise
                time.sleep(0.25)

    def await_go(self, run_id: str, phase: str, barrier_seconds: float) -> float:
        """Block until the platform releases this run. Returns the release time."""
        deadline = time.time() + barrier_seconds
        query = urllib.parse.urlencode({"run_id": run_id, "phase": phase})
        while True:
            try:
                self._call("GET", "/go?" + query, None, timeout=5.0)
                return time.time()
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            except (urllib.error.URLError, OSError):
                pass
            if time.time() > deadline:
                raise SystemExit("platform did not release the run in time")
            time.sleep(0.2)

    def after_response(self, report: dict) -> str:
        answer = self._call("POST", "/after-response", report)
        return answer.get("action", CONTINUE)

    def after_disposition(self, report: dict) -> str:
        answer = self._call("POST", "/after-disposition", report)
        return answer.get("action", CONTINUE)

    def announce_done(self, report: dict) -> str:
        answer = self._call("POST", "/done", report)
        return answer.get("action", "OK")


class Receiver:
    """Posts deliveries to the destination receiver."""

    def __init__(self, base_url: str, timeout_seconds: float):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def deliver(self, endpoint_id: str, events: list) -> tuple[int, str]:
        url = "%s/deliver/%s" % (self.base_url, urllib.parse.quote(endpoint_id))
        body = json.dumps({"events": events}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                payload = json.loads(error.read().decode("utf-8") or "{}")
            except Exception:
                payload = {}
        except (urllib.error.URLError, OSError):
            return 0, ""
        return status, payload.get("request_id", "")


class PendingStream:
    """The SQS stream of undelivered events for this destination.

    Every event of a destination is one message in one message group, so the
    queue hands the events over in the order they were accepted.
    """

    def __init__(self, config: RunConfig):
        self.queue_url = config.queue_url
        self.group = config.endpoint_id
        self.sqs = boto3.client("sqs", **config.aws)

    def receive(self, limit: int, wait_seconds: int) -> list:
        answer = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=limit,
            WaitTimeSeconds=wait_seconds,
            MessageAttributeNames=["All"],
        )
        return answer.get("Messages", [])

    def acknowledge(self, message: dict) -> None:
        """A delivered message leaves the stream."""
        self.sqs.delete_message(
            QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"]
        )

    def return_message(self, message: dict) -> None:
        """Give an undelivered message straight back to the stream."""
        self.sqs.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=message["ReceiptHandle"],
            VisibilityTimeout=0,
        )

    def append(self, event: dict, deduplication_id: str) -> str:
        """Put an event back on the stream behind everything already pending."""
        answer = self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(event, separators=(",", ":"), sort_keys=True),
            MessageGroupId=self.group,
            MessageDeduplicationId=deduplication_id,
        )
        return answer.get("MessageId", "")


def decode_event(message: dict) -> dict:
    return json.loads(message["Body"])


class Dispatcher:
    """Serves one run.

    One destination, one delivery in flight: take the head of the stream, hold
    it until the receiver acknowledges it, and only then take more. Work the
    receiver rejects is never removed from the stream — it is still the head,
    and it is taken again. Everything the run has not delivered stays on the
    stream, so a replacement process finds it exactly where it was.
    """

    def __init__(self, config: RunConfig):
        self.config = config
        self.control = ControlPlane(config.control_url)
        self.receiver = Receiver(config.receiver_url, config.http_timeout_seconds)
        self.stream = PendingStream(config)
        self.delivered = 0
        # Events the receiver has acknowledged while this process served the
        # run. Not a record of outstanding work — the stream is the only one of
        # those — just the guard that keeps an acknowledged event from being
        # delivered a second time when a record whose delete did not go through
        # comes round again.
        self.acknowledged: set = set()

    def identity(self) -> dict:
        return {
            "pid": os.getpid(),
            "run_id": self.config.run_id,
            "phase": self.config.phase,
        }

    def _settle(self, message: dict) -> bool:
        """Take a message the receiver has acknowledged off the stream.

        The handle in hand may already be spent: a report the control plane
        held open can outlive the receive's visibility, and that delete is
        then refused. That is not trouble — the record is back on the stream
        and the next receive hands it over on a handle that works. Sending it
        to the destination again is what would be wrong.
        """
        try:
            self.stream.acknowledge(message)
            return True
        except Exception as error:
            log(
                "delete with a spent handle deferred for %s (%s)"
                % (message.get("MessageId"), error)
            )
            return False

    def _report(self, request_id: str, status: int, held: list, events: list) -> dict:
        return {
            "run_id": self.config.run_id,
            "phase": self.config.phase,
            "endpoint_id": self.config.endpoint_id,
            "request_id": request_id,
            "status": status,
            "message_ids": [message["MessageId"] for message in held],
            "receipt_handles": [message["ReceiptHandle"] for message in held],
            "seqs": [event.get("seq") for event in events],
        }

    def _take(self) -> list:
        """Take the head of the destination's stream, at most a batch."""
        return self.stream.receive(
            self.config.delivery_batch_size, self.config.poll_wait_seconds
        )

    def serve_once(self) -> str:
        """Work the stream until nothing is left unresolved.

        Returns "drained" once the stream has come back empty for longer than
        one visibility window — long enough that any receive, this process's
        own or a stopped predecessor's, has given its record back — and
        "reloaded" when the platform re-opened the run.
        """
        held: list = []
        quiet_since = None
        while True:
            if not held:
                messages = self._take()
                if not messages:
                    if quiet_since is None:
                        quiet_since = time.time()
                    if (
                        time.time() - quiet_since
                        < self.config.visibility_seconds + VISIBILITY_SLACK_SECONDS
                    ):
                        continue
                    return "drained"
                quiet_since = None
                held = messages
            events = [decode_event(message) for message in held]
            outstanding = []
            for message, event in zip(held, events):
                if str(event.get("event_id")) in self.acknowledged:
                    # Already acknowledged, so never delivered again: settle
                    # the record with whatever handle works.
                    if not self._settle(message):
                        time.sleep(self.config.retry_backoff_seconds)
                    continue
                outstanding.append((message, event))
            if not outstanding:
                held = []
                continue
            deliverables = [event for _, event in outstanding]
            status, request_id = self.receiver.deliver(
                self.config.endpoint_id, deliverables
            )
            acknowledged = 200 <= status < 300
            log(
                "delivered seqs=%s status=%s"
                % ([event.get("seq") for event in deliverables], status)
            )
            response_action = self.control.after_response(
                self._report(
                    request_id,
                    status,
                    [message for message, _ in outstanding],
                    deliverables,
                )
            )
            if acknowledged:
                for message, event in outstanding:
                    self.acknowledged.add(str(event.get("event_id")))
                    self.delivered += 1
                    self._settle(message)
            else:
                for message, _ in outstanding:
                    self.stream.return_message(message)
            if response_action != RELOAD:
                disposition = self.control.after_disposition(
                    {
                        "run_id": self.config.run_id,
                        "phase": self.config.phase,
                        "request_id": request_id,
                    }
                )
                if disposition == RELOAD:
                    return "reloaded"
            held = []
            if not acknowledged:
                # The rejected events are still the head of the stream. Nothing
                # else for this destination moves until they are delivered.
                time.sleep(self.config.retry_backoff_seconds)
                continue

    def serve(self) -> int:
        self.control.announce_ready(self.identity())
        self.control.await_go(
            self.config.run_id, self.config.phase, self.config.barrier_seconds
        )
        log("run %s phase %s started" % (self.config.run_id, self.config.phase))
        while True:
            if self.serve_once() == "drained":
                action = self.control.announce_done(
                    {
                        "run_id": self.config.run_id,
                        "phase": self.config.phase,
                        "delivered": self.delivered,
                    }
                )
                if action == RELOAD:
                    log("stream re-opened, taking it from the top")
                    continue
                log("run %s finished, %d events delivered" % (self.config.run_id, self.delivered))
                return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dispatcher.py", description="Ordered-destination delivery service."
    )
    parser.add_argument("--config", required=True, help="path to the run configuration JSON")
    args = parser.parse_args(argv)
    config = RunConfig(args.config)
    return Dispatcher(config).serve()


if __name__ == "__main__":
    sys.exit(main())
