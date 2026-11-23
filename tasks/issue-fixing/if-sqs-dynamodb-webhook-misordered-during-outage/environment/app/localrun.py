#!/usr/bin/env python3
"""One clean local pass of the delivery service.

Stands up a throwaway FIFO stream, a recording receiver and an idle control
plane, runs the service over them once and prints counts. Smoke run only: every
delivery succeeds here, so this exercises the accept/deliver/acknowledge path
and nothing else.

    python3 localrun.py [--events 24] [--batch 4]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ACK_DELAY_SECONDS = 0.5
RUN_TIMEOUT_SECONDS = 180.0


def make_client(service: str):
    return boto3.client(
        service,
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
    )


class Recording:
    """What the receiver saw. Counts only; this is a smoke run."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.events: list[int] = []
        self.statuses: list[int] = []

    def note(self, seqs: list, status: int) -> None:
        with self.lock:
            self.requests += 1
            self.events.extend(seqs)
            self.statuses.append(status)


class LocalReceiverHandler(BaseHTTPRequestHandler):
    recording: Recording = None  # set on the server
    batch_limit = 4
    delay = ACK_DELAY_SECONDS

    def log_message(self, *args):  # silence the default per-request chatter
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        seqs = []
        for event in body.get("events", []):
            seqs.append(event.get("seq"))
        self.recording.note(seqs, 200)
        time.sleep(self.delay)
        payload = json.dumps({"request_id": uuid.uuid4().hex[:12]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class IdleControlHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        path = self.path.split("?")[0]
        if path == "/done":
            self._answer({"action": "OK"})
        else:
            self._answer({"action": "CONTINUE"})

    def do_GET(self):
        if self.path.split("?")[0] == "/go":
            self._answer({"go": True})
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(handler) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, "http://%s:%d" % (host, port)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="localrun.py", description=__doc__.splitlines()[0])
    parser.add_argument("--events", type=int, default=24, help="how many events to accept")
    parser.add_argument("--batch", type=int, default=4, help="delivery_batch_size")
    args = parser.parse_args(argv)

    sqs = make_client("sqs")
    token = uuid.uuid4().hex[:8]
    queue_name = "localrun-%s.fifo" % token
    queue = deploy(make_client("cloudformation"), "webhook-local-seed-" + token,
                   Path(__file__).parent / "infra/seed.template.json", QueueName=queue_name)
    queue_url = queue["QueueUrl"]
    endpoint_id = "dst-local-%s" % token
    run_id = "local-%s" % token

    receiver_handler = type(
        "Handler",
        (LocalReceiverHandler,),
        {"recording": Recording(), "batch_limit": args.batch},
    )
    receiver_server, receiver_url = serve(receiver_handler)
    control_server, control_url = serve(IdleControlHandler)
    recording = receiver_handler.recording

    workdir = tempfile.mkdtemp(prefix="localrun-")
    try:
        for seq in range(1, args.events + 1):
            event = {
                "event_id": "evt-%s-%04d" % (token, seq),
                "seq": seq,
                "payload_nonce": uuid.uuid4().hex,
                "payload": {"seq": seq},
            }
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(event, separators=(",", ":"), sort_keys=True),
                MessageGroupId=endpoint_id,
                MessageDeduplicationId="%s-%04d" % (token, seq),
            )

        config_path = os.path.join(workdir, "run.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "run_id": run_id,
                    "phase": "local",
                    "endpoint_id": endpoint_id,
                    "queue_url": queue_url,
                    "receiver_url": receiver_url,
                    "control_url": control_url,
                    "delivery_batch_size": args.batch,
                    "http_timeout_seconds": 2.0,
                    "poll_wait_seconds": 1,
                    "retry_backoff_seconds": 1.0,
                    "visibility_seconds": 8,
                    "barrier_seconds": 5.0,
                },
                handle,
            )

        started = time.time()
        worker = subprocess.Popen(
            [sys.executable, os.path.join(APP_DIR, "dispatcher.py"), "--config", config_path],
            cwd=workdir,
            env=dict(os.environ, PYTHONPATH=APP_DIR),
        )
        try:
            rc = worker.wait(timeout=RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
            print("service did not finish within %.0fs" % RUN_TIMEOUT_SECONDS)
            rc = -9
        elapsed = time.time() - started

        leftover = sqs.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1
        ).get("Messages", [])
        print(
            "receiver accepted %d events across %d requests (statuses %s); service exit %d "
            "after %.1fs; stream drained: %s"
            % (
                len(recording.events),
                recording.requests,
                sorted(set(recording.statuses)),
                rc,
                elapsed,
                "yes" if not leftover else "no, %d left" % len(leftover),
            )
        )
        return 0 if rc == 0 and not leftover else 1
    finally:
        receiver_server.shutdown()
        control_server.shutdown()
        try:
            sqs.delete_queue(QueueUrl=queue_url)
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
