"""Client for the cluster API (the subset of the Kubernetes API that NTH uses).

Endpoints, all JSON, all authenticated with the pod's bearer token:

    GET  /api/v1/nodes[?instanceId=<id>]               {"items": [node, ...]}
    GET  /api/v1/nodes/<name>                          node | 404
    POST /api/v1/nodes/<name>/cordon                   node (idempotent)
    GET  /api/v1/nodes/<name>/pods                     {"items": [{"name", "namespace"}, ...]}
    POST /api/v1/namespaces/<ns>/pods/<pod>/eviction   201 once the pod is gone
                                                       404 pod already gone
                                                       429 disruption budget / load shedding

A node is {"name", "instanceID", "providerID", "unschedulable", "podCount"}.
"""

import http.client
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("nth.kube")

# An eviction is answered within ~4 s; a call with no answer after this is a dead connection.
REQUEST_TIMEOUT_SEC = 10


class KubeError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(f"cluster API returned {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class NotFound(KubeError):
    pass


class TooManyRequests(KubeError):
    pass


class Unreachable(Exception):
    """The cluster API gave no answer at all (dead connection, timeout, reset)."""


class ClusterClient:
    def __init__(self, base_url, token, pod_name):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.pod_name = pod_name
        # False from the first unanswered call until the API answers again.
        self.reachable = True

    def _request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("User-Agent", f"aws-node-termination-handler/{self.pod_name}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
                raw = response.read()
                self.reachable = True
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as err:
            self.reachable = True
            raw = err.read()
            try:
                message = json.loads(raw).get("message", "") if raw else ""
            except ValueError:
                message = raw.decode(errors="replace")
            retry_after = err.headers.get("Retry-After")
            retry_after = float(retry_after) if retry_after else None
            if err.code == 404:
                raise NotFound(err.code, message) from None
            if err.code == 429:
                raise TooManyRequests(err.code, message, retry_after) from None
            raise KubeError(err.code, message, retry_after) from None
        except (OSError, http.client.HTTPException) as err:
            self.reachable = False
            raise Unreachable(f"no answer from the cluster API for {method} {path}: {err}") from None

    def list_nodes(self):
        _, body = self._request("GET", "/api/v1/nodes")
        return body.get("items", [])

    def node_for_instance(self, instance_id):
        """The node backed by this EC2 instance, or None if it is not part of this cluster."""
        _, body = self._request("GET", f"/api/v1/nodes?instanceId={instance_id}")
        items = body.get("items", [])
        return items[0] if items else None

    def get_node(self, name):
        _, body = self._request("GET", f"/api/v1/nodes/{name}")
        return body

    def cordon(self, name):
        _, body = self._request("POST", f"/api/v1/nodes/{name}/cordon", {})
        return body

    def list_pods(self, node_name):
        _, body = self._request("GET", f"/api/v1/nodes/{node_name}/pods")
        return body.get("items", [])

    def evict(self, namespace, pod):
        self._request("POST", f"/api/v1/namespaces/{namespace}/pods/{pod}/eviction", {})

    def cordon_and_drain(self, node_name, timeout_sec):
        """Cordon the node, then evict its pods one at a time, honouring disruption budgets."""
        deadline = time.monotonic() + timeout_sec
        self.cordon(node_name)
        pods = self.list_pods(node_name)
        log.info("draining node %s: %d pods", node_name, len(pods))
        for pod in pods:
            while True:
                if time.monotonic() > deadline:
                    raise KubeError(504, f"drain of {node_name} timed out after {timeout_sec}s")
                try:
                    self.evict(pod["namespace"], pod["name"])
                    log.info("evicted pod %s/%s from %s", pod["namespace"], pod["name"], node_name)
                    break
                except NotFound:
                    break
                except TooManyRequests as err:
                    # Same behaviour as kubectl drain: the eviction would violate a
                    # disruption budget, wait and try again.
                    time.sleep(err.retry_after or 1.0)
