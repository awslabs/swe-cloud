"""Verifier-side account gateway: forwards to Floci, enforces protection, injects faults, audits.

Runs in the verifier's own process (threads), so the audit never touches the solver's files.
Every request is recorded with monotonic start/end timestamps from one clock; the end timestamp is
taken after the response has been flushed to the client.
"""
import http.client
import http.server
import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

PROTECTION_TAG = "DeletionProtection"
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "expect", "proxy-connection", "upgrade"}
_SCOPE = re.compile(r"Credential=[^/]+/\d{8}/[^/]+/([^/]+)/aws4_request")


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


class Gateway:
    def __init__(self, upstream_host, upstream_port, direct):
        self.upstream = (upstream_host, upstream_port)
        self.direct = direct  # dict of boto3 clients straight to Floci
        self.lock = threading.Lock()
        self.audit = []
        self.run_label = None
        # faults
        self.throttle = {}         # secret name -> remaining ThrottlingException answers to DeleteSecret
        self.s3_read_fault = {}    # bucket -> remaining 503 SlowDown answers to object reads (HEAD/GET)
        self.secret_arns = {}      # arn -> name for secrets the verifier created
        self.server = None

    # -- control -----------------------------------------------------------------------------

    def start(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def set_run(self, label, throttle=None, s3_read_fault=None):
        with self.lock:
            self.run_label = label
            self.throttle = dict(throttle or {})
            self.s3_read_fault = dict(s3_read_fault or {})

    def clear_s3_fault(self):
        with self.lock:
            self.s3_read_fault = {}

    def record(self, entry):
        with self.lock:
            entry["run"] = self.run_label
            self.audit.append(entry)

    # -- helpers -----------------------------------------------------------------------------

    def secret_name(self, secret_id):
        if secret_id in self.secret_arns:
            return self.secret_arns[secret_id]
        if secret_id.startswith("arn:"):
            try:
                return self.direct["secretsmanager"].describe_secret(SecretId=secret_id)["Name"]
            except Exception:
                return secret_id
        return secret_id

    def secret_protected(self, secret_id):
        try:
            tags = self.direct["secretsmanager"].describe_secret(SecretId=secret_id).get("Tags", [])
        except Exception:
            return False
        return any(t.get("Key") == PROTECTION_TAG and str(t.get("Value", "")).lower() == "true" for t in tags)

    def take_throttle(self, name):
        with self.lock:
            left = self.throttle.get(name, 0)
            if left > 0:
                self.throttle[name] = left - 1
                return True
        return False

    def take_s3_fault(self, bucket):
        with self.lock:
            left = self.s3_read_fault.get(bucket, 0)
            if left > 0:
                self.s3_read_fault[bucket] = left - 1
                return True
        return False

    def forward(self, method, path, headers, body):
        conn = http.client.HTTPConnection(*self.upstream, timeout=60)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp.read()
        finally:
            conn.close()


def _json_error(code, message, status=400):
    body = json.dumps({"__type": code, "message": message}).encode()
    return status, [("Content-Type", "application/x-amz-json-1.1"), ("x-amzn-ErrorType", code)], body


def _s3_error(code, message, status):
    body = (f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code><Message>{message}'
            f'</Message><RequestId>gateway</RequestId></Error>').encode()
    return status, [("Content-Type", "application/xml")], body


def _parse_s3(method, host, raw_path, headers, body):
    split = urllib.parse.urlsplit(raw_path)
    path = urllib.parse.unquote(split.path)
    query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
    hostname = (host or "").split(":")[0]
    if hostname.endswith(".localhost") and hostname not in ("localhost",):
        bucket = hostname[: -len(".localhost")]
        key = path.lstrip("/")
    else:
        parts = path.lstrip("/").split("/", 1)
        bucket = parts[0] or None
        key = parts[1] if len(parts) > 1 else ""
    keys = []
    if not bucket:
        action = "ListBuckets"
    elif not key:
        if method == "GET":
            action = "ListObjects" if ("list-type" in query or not query) else "GetBucket:" + ",".join(sorted(query))
        elif method == "POST" and "delete" in query:
            action = "DeleteObjects"
            try:
                root = ET.fromstring(body)
                keys = [el.text for el in root.iter() if _local_name(el.tag) == "Key"]
            except ET.ParseError:
                keys = []
        else:
            action = {"PUT": "CreateBucket", "DELETE": "DeleteBucket", "HEAD": "HeadBucket"}.get(method, method + "Bucket")
    else:
        if method == "PUT" and headers.get("x-amz-copy-source"):
            action = "CopyObject"
        else:
            action = {"GET": "GetObject", "HEAD": "HeadObject", "PUT": "PutObject",
                      "DELETE": "DeleteObject", "POST": "PostObject"}.get(method, method + "Object")
    return bucket, key, action, keys


def _make_handler(gw):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

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
            t_start = time.monotonic_ns()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            head = self.command == "HEAD"
            target = self.headers.get("X-Amz-Target", "")
            scope = _SCOPE.search(self.headers.get("Authorization", ""))
            service = scope.group(1) if scope else ("s3" if not target else "unknown")
            entry = {"t_start": t_start, "service": service, "method": self.command, "path": self.path}

            response = None
            if target:
                action = target.split(".")[-1]
                entry["action"] = action
                try:
                    request = json.loads(body or b"{}")
                except ValueError:
                    request = {}
                if service == "secretsmanager":
                    sid = request.get("SecretId", "")
                    entry["resource"] = gw.secret_name(sid) if sid else request.get("Name", "")
                    if action == "DeleteSecret":
                        if gw.secret_protected(sid):
                            response = _json_error(
                                "InvalidRequestException",
                                "Cannot delete protected secret, please disable deletion protection and try again.")
                        elif gw.take_throttle(entry["resource"]):
                            response = _json_error("ThrottlingException", "Rate exceeded")
                    elif action == "TagResource" and any(t.get("Key") == PROTECTION_TAG for t in request.get("Tags", [])):
                        response = _json_error("AccessDeniedException", "DeletionProtection is managed by the owning team.")
                    elif action == "UntagResource" and PROTECTION_TAG in request.get("TagKeys", []):
                        response = _json_error("AccessDeniedException", "DeletionProtection is managed by the owning team.")
                elif service == "kms":
                    entry["resource"] = request.get("KeyId", "")
            else:
                bucket, key, action, keys = _parse_s3(self.command, self.headers.get("Host"), self.path,
                                                      {k.lower(): v for k, v in self.headers.items()}, body)
                entry.update(action=action, bucket=bucket, key=key)
                if keys:
                    entry["keys"] = keys
                if bucket and key and self.command in ("GET", "HEAD") and gw.take_s3_fault(bucket):
                    response = _s3_error("SlowDown", "Please reduce your request rate.", 503)

            if response is None:
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
                try:
                    response = gw.forward(self.command, self.path, headers, body)
                except Exception as err:  # upstream unreachable
                    response = _s3_error("InternalError", str(err), 500) if service == "s3" else \
                        _json_error("InternalFailure", str(err), 500)
                else:
                    entry["forwarded"] = True
                    if service == "kms" and entry.get("action") == "CancelKeyDeletion" and response[0] == 200:
                        try:
                            gw.direct["kms"].disable_key(KeyId=json.loads(response[2]).get("KeyId"))
                        except Exception:
                            pass
                    if entry.get("action") == "DeleteObjects" and response[0] == 200:
                        try:
                            root = ET.fromstring(response[2])
                            deleted = []
                            for el in root:
                                if _local_name(el.tag) == "Deleted":
                                    for sub in el:
                                        if _local_name(sub.tag) == "Key":
                                            deleted.append(sub.text)
                            entry["deleted_keys"] = deleted
                        except ET.ParseError:
                            entry["deleted_keys"] = []

            status = response[0]
            entry["status"] = status
            if status >= 300 and response[2]:
                try:
                    entry["error"] = json.loads(response[2]).get("__type", "")
                except ValueError:
                    m = re.search(rb"<Code>([^<]+)</Code>", response[2])
                    entry["error"] = m.group(1).decode() if m else ""
            try:
                self._send(*response, head=head)
            finally:
                entry["t_end"] = time.monotonic_ns()
                gw.record(entry)

        do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = do_PATCH = _handle

    return Handler
