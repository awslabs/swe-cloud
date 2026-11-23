"""Notify delivery-status callback endpoint (port of /api/notify-callback).

GC Notify POSTs a delivery receipt for every notification we send. Using the delivery status
it decides whether a submission has to be reprocessed by the reliability worker.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, log, store

CALLBACK_PATH = "/api/notify-callback"
FINAL_STATUSES = ("delivered", "permanent-failure")


def handle_status(body):
    """Returns (http_status, payload)."""
    submission_id = body.get("reference")
    delivery_status = body.get("status")
    if submission_id is None or delivery_status is None:
        return 400, {"error": "Invalid payload: missing key attributes (reference or status)"}

    if delivery_status in FINAL_STATUSES:
        return 200, {"status": "submission will not be reprocessed"}

    # Remove previous process completion identifier
    store.remove_processed_mark(submission_id)
    config.sqs().send_message(
        QueueUrl=config.reprocess_queue_url(),
        MessageBody=json.dumps({"submissionID": submission_id}),
    )
    log.info(
        submissionId=submission_id,
        message=f"Notify Callback: Reprocessing submission due to notify status of: {delivery_status}",
    )
    return 200, {"status": "submission will be reprocessed"}


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "gcforms-notify-callback"

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.split("?")[0] != CALLBACK_PATH:
            return self._reply(404, {"error": "not found"})
        if self.headers.get("Authorization", "") != f"Bearer {config.NOTIFY_CALLBACK_TOKEN}":
            return self._reply(403, {"error": "Invalid authorization header"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._reply(400, {"error": "Invalid JSON"})
        if body.get("health_check") == "true":
            return self._reply(200, {"status": "ok"})
        try:
            status, payload = handle_status(body)
        except Exception as error:
            log.warn(message="Notify Callback failed", error=repr(error), body=body)
            return self._reply(500, {"responses": "Processing error on the server side"})
        return self._reply(status, payload)

    def log_message(self, fmt, *args):
        log.info(message="callback request", line=fmt % args)


def main(port):
    # Build the AWS clients before serving: requests are handled on many threads, and Notify
    # gives us one second to answer.
    config.dynamodb()
    config.sqs()
    config.reprocess_queue_url()
    server = ThreadingHTTPServer(("127.0.0.1", port), CallbackHandler)
    server.daemon_threads = True
    log.info(message="callback server listening", port=port, path=CALLBACK_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
