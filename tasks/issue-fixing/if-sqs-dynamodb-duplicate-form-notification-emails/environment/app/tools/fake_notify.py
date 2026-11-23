"""A local stand-in for the GC Notify API, for development.

Implements the endpoints in docs/notify-api.md: every POST creates a new notification, the list
endpoint filters by exact reference, and a delivery receipt is POSTed to the callback URL a
moment after each send. Every email here is delivered; nothing is ever slow or lost.

    python3 tools/fake_notify.py [--port 8025] [--callback-url http://127.0.0.1:8026/api/notify-callback]
    curl -s localhost:8025/_dev/notifications      # everything "sent" so far
"""
import argparse
import datetime
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
NOTIFICATIONS = []  # newest last


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def post_callback(url, token, notification, status):
    payload = {
        "id": notification["id"],
        "reference": notification["reference"],
        "to": notification["email_address"],
        "status": status,
        "status_description": "Delivered" if status == "delivered" else "In transit",
        "provider_response": None,
        "created_at": notification["created_at"],
        "completed_at": now(),
        "sent_at": now(),
        "notification_type": "email",
    }
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {token}")
    for _ in range(7):
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    return
        except Exception:
            pass
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    callback_url = None
    callback_token = None

    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/v2/notifications/email":
            return self._reply(404, {"errors": [{"error": "NotFound"}]})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not body.get("email_address") or not body.get("template_id"):
            return self._reply(400, {"errors": [{"error": "ValidationError"}]})
        notification = {
            "id": str(uuid.uuid4()),
            "reference": body.get("reference"),
            "email_address": body["email_address"],
            "type": "email",
            "status": "created",
            "created_at": now(),
            "personalisation": body.get("personalisation") or {},
        }
        with LOCK:
            NOTIFICATIONS.append(notification)
        self._reply(201, {"id": notification["id"], "reference": notification["reference"],
                          "content": {"subject": notification["personalisation"].get("subject")}})
        if self.callback_url:
            threading.Thread(target=self._deliver, args=(notification,), daemon=True).start()

    def _deliver(self, notification):
        time.sleep(1)
        with LOCK:
            notification["status"] = "delivered"
        post_callback(self.callback_url, self.callback_token, notification, "delivered")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        with LOCK:
            items = [dict(n) for n in reversed(NOTIFICATIONS)]
        if parsed.path == "/_dev/notifications":
            return self._reply(200, {"notifications": items})
        if parsed.path == "/v2/notifications":
            if "reference" in query:
                items = [n for n in items if n["reference"] == query["reference"][0]]
            if "status" in query:
                items = [n for n in items if n["status"] == query["status"][0]]
            return self._reply(200, {"notifications": items[:250]})
        if parsed.path.startswith("/v2/notifications/"):
            wanted = parsed.path.rsplit("/", 1)[1]
            for n in items:
                if n["id"] == wanted:
                    return self._reply(200, n)
            return self._reply(404, {"errors": [{"error": "NoResultFound"}]})
        return self._reply(404, {"errors": [{"error": "NotFound"}]})

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8025)
    parser.add_argument("--callback-url", default="http://127.0.0.1:8026/api/notify-callback")
    args = parser.parse_args()
    Handler.callback_url = args.callback_url
    Handler.callback_token = os.environ.get("NOTIFY_CALLBACK_TOKEN", "gcforms-local-callback-token")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(f"fake notify on 127.0.0.1:{args.port}, callbacks to {args.callback_url}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
