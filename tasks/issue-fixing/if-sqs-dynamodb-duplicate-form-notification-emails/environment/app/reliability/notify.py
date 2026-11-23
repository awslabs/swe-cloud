"""Minimal GC Notify API client (a port of the calls we use from notifications-node-client).

See docs/notify-api.md for the endpoints and their semantics.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class NotifyError(Exception):
    def __init__(self, status, body):
        super().__init__(f"GC Notify errored with status code {status}: {body}")
        self.status = status
        self.body = body


class NotifyClient:
    def __init__(self, base_url, api_key, worker_id=None, timeout=30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.worker_id = worker_id
        self.timeout = timeout

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"ApiKey-v1 {self.api_key}")
        request.add_header("Content-Type", "application/json")
        if self.worker_id:
            # Lets Notify support correlate API calls with our worker logs.
            request.add_header("X-Worker-Id", self.worker_id)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            raise NotifyError(error.code, error.read().decode(errors="replace")) from None

    def send_email(self, template_id, email_address, personalisation=None, reference=None):
        payload = {"template_id": template_id, "email_address": email_address}
        if personalisation:
            payload["personalisation"] = personalisation
        if reference is not None:
            payload["reference"] = reference
        return self._request("POST", "/v2/notifications/email", payload)

    def get_notification(self, notification_id):
        return self._request("GET", f"/v2/notifications/{urllib.parse.quote(notification_id)}")

    def get_notifications(self, reference=None, status=None, template_type=None, older_than=None):
        query = {
            key: value
            for key, value in (
                ("reference", reference),
                ("status", status),
                ("template_type", template_type),
                ("older_than", older_than),
            )
            if value is not None
        }
        path = "/v2/notifications"
        if query:
            path += "?" + urllib.parse.urlencode(query)
        return self._request("GET", path).get("notifications", [])
