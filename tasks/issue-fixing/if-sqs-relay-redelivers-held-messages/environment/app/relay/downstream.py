"""The downstream the relay hands each message to (see README.md, "Downstream")."""

import json
import urllib.error
import urllib.request


class DownstreamError(Exception):
    pass


class Downstream:

    def __init__(self, url, relay_id, timeout=600):
        self.url = url
        self.relay_id = relay_id
        self.timeout = timeout

    def deliver(self, delivery):
        """POSTs the message and blocks until the downstream answers. Raises unless it answers 2xx."""
        payload = json.dumps({
            "relay": self.relay_id,
            "message_id": delivery.message_id,
            "receipt_handle": delivery.receipt_handle,
            "body": delivery.body,
        }).encode()
        request = urllib.request.Request(
            self.url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raise DownstreamError(f"downstream answered {error.code} for {delivery.message_id}") from None
        except (urllib.error.URLError, OSError) as error:
            raise DownstreamError(f"downstream unreachable for {delivery.message_id}: {error}") from None
        if not 200 <= status < 300:
            raise DownstreamError(f"downstream answered {status} for {delivery.message_id}")
