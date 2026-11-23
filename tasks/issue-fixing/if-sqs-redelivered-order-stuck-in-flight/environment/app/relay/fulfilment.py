import json
import os
import urllib.error
import urllib.request


class FulfilmentFailed(Exception):
    pass


class FulfilmentClient:
    """POSTs one order to the fulfilment service. 2xx is success; anything else is a fault."""

    def __init__(self, settings):
        self._url = settings.work_url
        self._timeout = settings.work_timeout_seconds

    def __call__(self, context):
        payload = json.dumps(
            {"message_id": context.message_id, "receive_count": context.receive_count, "body": context.body}
        ).encode()
        request = urllib.request.Request(
            self._url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "X-Relay-Pid": str(os.getpid())},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response.read()
        except urllib.error.HTTPError as error:
            raise FulfilmentFailed(f"fulfilment answered {error.code}") from None
        except OSError as error:
            raise FulfilmentFailed(f"fulfilment unreachable: {error}") from None
