#!/usr/bin/env python3
"""Sandbox account gateway.

The scheduled sweep does not talk to the AWS endpoint directly: it runs with AWS_ENDPOINT_URL
pointing at this gateway, which forwards every request to the endpoint behind it and enforces
the account's guard rails on the way:

  * Deletion protection. A secret tagged DeletionProtection=true cannot be deleted
    (InvalidRequestException "Cannot delete protected secret, please disable deletion protection
    and try again."), the same way RDS refuses to delete a protected DB instance. Protection is
    owned by the team that owns the secret: the DeletionProtection tag cannot be added, changed
    or removed through the gateway.
  * KMS CancelKeyDeletion leaves the key Disabled, as AWS documents. (The local emulator would
    put it straight back to Enabled.)

    python3 /opt/sandbox/gateway.py [--port 4567] [--upstream http://localhost:4566]
"""
import argparse
import http.client
import http.server
import json
import threading
import urllib.parse

import boto3
from botocore.config import Config

PROTECTION_TAG = "DeletionProtection"
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "expect", "proxy-connection", "upgrade"}


class Gateway:
    def __init__(self, upstream):
        self.upstream = urllib.parse.urlsplit(upstream)
        cfg = Config(retries={"total_max_attempts": 1})
        self._lock = threading.Lock()
        self._sm = boto3.client("secretsmanager", endpoint_url=upstream, region_name="us-east-1",
                                aws_access_key_id="test", aws_secret_access_key="test", config=cfg)
        self._kms = boto3.client("kms", endpoint_url=upstream, region_name="us-east-1",
                                 aws_access_key_id="test", aws_secret_access_key="test", config=cfg)

    def secret_protected(self, secret_id):
        try:
            with self._lock:
                tags = self._sm.describe_secret(SecretId=secret_id).get("Tags", [])
        except Exception:
            return False
        return any(t.get("Key") == PROTECTION_TAG and str(t.get("Value", "")).lower() == "true" for t in tags)

    def disable_key(self, key_id):
        with self._lock:
            self._kms.disable_key(KeyId=key_id)

    def forward(self, method, path, headers, body):
        conn = http.client.HTTPConnection(self.upstream.hostname, self.upstream.port or 80, timeout=60)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.getheaders(), data
        finally:
            conn.close()


def json_error(code, message, status=400):
    body = json.dumps({"__type": code, "message": message}).encode()
    return status, [("Content-Type", "application/x-amz-json-1.1"), ("x-amzn-ErrorType", code)], body


def make_handler(gateway):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _send(self, status, headers, body, head=False):
            self.send_response(status)
            has_length = False
            for name, value in headers:
                lname = name.lower()
                if lname in HOP_HEADERS:
                    continue
                if lname == "content-length":
                    if not head:
                        continue
                    has_length = True
                self.send_header(name, value)
            if not head:
                self.send_header("Content-Length", str(len(body)))
            elif not has_length:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if not head and body:
                self.wfile.write(body)
            self.wfile.flush()

        def _handle(self):
            body = self._body()
            target = self.headers.get("X-Amz-Target", "")
            action = target.split(".")[-1] if target else ""
            request = {}
            if target:
                try:
                    request = json.loads(body or b"{}")
                except ValueError:
                    request = {}

            if target.startswith("secretsmanager."):
                if action == "DeleteSecret" and gateway.secret_protected(request.get("SecretId", "")):
                    return self._send(*json_error(
                        "InvalidRequestException",
                        "Cannot delete protected secret, please disable deletion protection and try again."))
                if action == "TagResource" and any(t.get("Key") == PROTECTION_TAG for t in request.get("Tags", [])):
                    return self._send(*json_error("AccessDeniedException",
                                                  "DeletionProtection is managed by the owning team."))
                if action == "UntagResource" and PROTECTION_TAG in request.get("TagKeys", []):
                    return self._send(*json_error("AccessDeniedException",
                                                  "DeletionProtection is managed by the owning team."))

            headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP_HEADERS]
            status, resp_headers, data = gateway.forward(self.command, self.path, dict(headers), body)

            if target.startswith("TrentService.") and action == "CancelKeyDeletion" and status == 200:
                try:
                    gateway.disable_key(json.loads(data).get("KeyId") or request.get("KeyId"))
                except Exception:
                    pass

            self._send(status, resp_headers, data, head=self.command == "HEAD")

        do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = do_PATCH = _handle

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4567)
    parser.add_argument("--upstream", default="http://localhost:4566")
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Gateway(args.upstream)))
    server.daemon_threads = True
    print(f"gateway listening on 127.0.0.1:{args.port} -> {args.upstream}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
