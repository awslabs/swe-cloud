"""A minimal code-host REST client for the calls the scale-up function makes.

Stands in for the Octokit installation client. Like Octokit with @octokit/plugin-retry it retries
5xx answers a few times before raising.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any

from . import config

MAX_ATTEMPTS = 3
BASE_DELAY_S = 0.05
REQUEST_TIMEOUT_S = 2.0


class HttpError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"HttpError {status}: {message}")
        self.status = status


class GitHubClient:
    def __init__(self, installation_id: int, base_url: str | None = None) -> None:
        self.installation_id = installation_id
        self.base_url = (base_url or config.github_api_url()).rstrip("/")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"token installation-{self.installation_id}",
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as http_error:
                error = HttpError(http_error.code, http_error.reason or "")
                retryable = http_error.code >= 500
            except (urllib.error.URLError, TimeoutError, ConnectionError) as network_error:
                error = HttpError(None, str(network_error))
                retryable = True
            if not retryable or attempt >= MAX_ATTEMPTS:
                raise error
            time.sleep(random.uniform(0, BASE_DELAY_S * 2 ** (attempt - 1)))

    def generate_runner_jitconfig_for_org(self, org: str, name: str, runner_group_id: int, labels: list[str]) -> dict[str, Any]:
        """POST /orgs/{org}/actions/runners/generate-jitconfig"""
        return self._request(
            "POST",
            f"/orgs/{org}/actions/runners/generate-jitconfig",
            {"name": name, "runner_group_id": runner_group_id, "labels": labels},
        )
