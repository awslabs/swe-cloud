#!/usr/bin/env python3
"""Local append service for the delivery output stream.

Every published message reaches the output stream through this service. It
accepts one append request at a time, in arrival order, and writes exactly one
output record per accepted request. It keeps no per-message state and offers no
per-message lookup: the response to a request carries the sequence number the
stream assigned to the record it wrote, and `GET /pending` reports how many
requests it has accepted without having answered them yet.

See /app/CONTRACT.md for the request and response shapes.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config


class AppendService:
    def __init__(self, config):
        self._kinesis = boto3.client(
            "kinesis",
            endpoint_url=config["endpoint"],
            region_name=config["region"],
            aws_access_key_id=config["access_key_id"],
            aws_secret_access_key=config["secret_access_key"],
            config=Config(
                retries={"total_max_attempts": int(config["sdk_total_attempts"])},
                connect_timeout=int(config["api_timeout_seconds"]),
                read_timeout=int(config["api_timeout_seconds"]),
            ),
        )
        self._stream = config["output_stream"]
        self._partition_key = config["partition_id"]
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._last_sequence = None
        self._accepted = 0
        self.appended = []

    def append(self, record):
        """Write one record to the output stream; returns its sequence number.

        Appends are serialized by the lock and this service is the only writer
        the stream has, so the order the records arrive in is the order the
        stream holds them in. A request counts as accepted until its record is
        written and its answer is on the way back.
        """
        with self._state_lock:
            self._accepted += 1
        try:
            with self._lock:
                response = self._kinesis.put_record(
                    StreamName=self._stream,
                    Data=json.dumps(record).encode("utf-8"),
                    PartitionKey=self._partition_key,
                )
                sequence = response["SequenceNumber"]
                self._last_sequence = sequence
                self.appended.append({"sequence": sequence, "record": record})
                return sequence
        finally:
            with self._state_lock:
                self._accepted -= 1

    def last_boundary(self):
        with self._lock:
            for entry in reversed(self.appended):
                if entry["record"].get("kind") == "BARRIER":
                    return entry
            return None

    def pending(self):
        """Requests accepted whose records are not written and answered yet."""
        with self._state_lock:
            return self._accepted


class Handler(BaseHTTPRequestHandler):
    service = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path != "/publish":
            self._reply(404, {"status": "unknown", "path": self.path})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._reply(400, {"status": "malformed"})
            return
        record = {
            "kind": "DATA",
            "run_id": request.get("run_id"),
            "partition_id": request.get("partition_id"),
            "epoch": request.get("epoch"),
            "input_sequence": request.get("input_sequence"),
            "messageId": request.get("messageId"),
            "payload": request.get("payload"),
        }
        try:
            sequence = self.service.append(record)
        except Exception as error:  # service failure, not a protocol answer
            self._reply(502, {"status": "failed", "error": str(error)})
            return
        self._reply(200, {"status": "appended", "sequence": sequence})

    def do_GET(self):
        if self.path == "/pending":
            self._reply(200, {"status": "ok", "accepted": self.service.pending()})
            return
        if self.path == "/recovery-boundary":
            boundary = self.service.last_boundary()
            if boundary is None:
                self._reply(200, {"status": "none"})
            else:
                self._reply(
                    200,
                    {
                        "status": "boundary",
                        "barrier_id": boundary["record"].get("barrier_id"),
                        "sequence": boundary["sequence"],
                    },
                )
            return
        self._reply(404, {"status": "unknown", "path": self.path})


def serve(config, host="127.0.0.1", port=0):
    service = AppendService(config)
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, service


def main(argv=None):
    parser = argparse.ArgumentParser(description="local append service")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    with open(args.config, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    server, _ = serve(config, port=args.port)
    print(json.dumps({"listening_on": server.server_address[1]}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
