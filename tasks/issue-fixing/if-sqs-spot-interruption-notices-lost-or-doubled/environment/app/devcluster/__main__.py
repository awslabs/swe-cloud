"""Serve the cluster API NTH talks to, backed by an in-memory cluster.

    python3 -m devcluster --port 8701 --node ip-10-0-1-17.ec2.internal=i-0f1e2d3c4b5a69780:10

Same endpoints, auth, disruption-budget and eviction behaviour as the cluster API the
handler runs against in production (see nth/kube.py for the endpoint list):

  * every request needs `Authorization: Bearer <token>` (any token from --token);
  * an eviction takes --evict-seconds on the server, whether or not the client waits;
  * every workload has a PodDisruptionBudget allowing one disruption at a time, so a second
    eviction on a node while one is in flight gets 429 with Retry-After.
"""

import argparse
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WORKLOADS = ["checkout", "cart", "search", "ledger", "render", "ingest", "notify", "quote"]


class Cluster:
    def __init__(self, nodes, evict_seconds):
        self.lock = threading.Lock()
        self.evict_seconds = evict_seconds
        self.nodes = {}
        for name, instance_id, pod_count in nodes:
            pods = [f"{WORKLOADS[i % len(WORKLOADS)]}-{secrets.token_hex(3)}-{i}"
                    for i in range(pod_count)]
            self.nodes[name] = {"name": name, "instanceID": instance_id,
                                "providerID": f"aws:///us-east-1b/{instance_id}",
                                "unschedulable": False, "pods": pods, "evicting": False}

    @staticmethod
    def view(node):
        return {k: node[k] for k in ("name", "instanceID", "providerID", "unschedulable")} | {
            "podCount": len(node["pods"])}


def make_handler(cluster, tokens):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body=None, headers=None):
            raw = json.dumps(body if body is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _caller(self):
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            return tokens.get(token)

        def _route(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            caller = self._caller()
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            if caller is None:
                status, body, headers = 401, {"message": "Unauthorized"}, None
            else:
                status, body, headers = self._dispatch(method, parts, parse_qs(url.query))
            print(f"{time.strftime('%H:%M:%S')} {caller or '-'} {method} {url.path} {status}",
                  flush=True)
            try:
                self._send(status, body, headers)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _dispatch(self, method, parts, query):
            with cluster.lock:
                nodes = cluster.nodes
                if method == "GET" and parts == ["api", "v1", "nodes"]:
                    items = list(nodes.values())
                    if "instanceId" in query:
                        items = [n for n in items if n["instanceID"] == query["instanceId"][0]]
                    return 200, {"items": [cluster.view(n) for n in items]}, None
                if len(parts) >= 4 and parts[:3] == ["api", "v1", "nodes"]:
                    node = nodes.get(parts[3])
                    if node is None:
                        return 404, {"message": f"node {parts[3]} not found"}, None
                    if method == "GET" and len(parts) == 4:
                        return 200, cluster.view(node), None
                    if method == "POST" and parts[4:] == ["cordon"]:
                        node["unschedulable"] = True
                        return 200, cluster.view(node), None
                    if method == "GET" and parts[4:] == ["pods"]:
                        return 200, {"items": [{"name": p, "namespace": "default"}
                                               for p in node["pods"]]}, None
                is_eviction = (method == "POST" and len(parts) == 7 and parts[:3] == ["api", "v1", "namespaces"]
                               and parts[4] == "pods" and parts[6] == "eviction")
                if not is_eviction:
                    return 404, {"message": "not found"}, None
                pod = parts[5]
                node = next((n for n in nodes.values() if pod in n["pods"]), None)
                if node is None:
                    return 404, {"message": f"pod {pod} not found"}, None
                if node["evicting"]:
                    return 429, {"message": "Cannot evict pod as it would violate the pod's "
                                            "disruption budget."}, {"Retry-After": "1"}
                node["evicting"] = True
            time.sleep(cluster.evict_seconds)
            with cluster.lock:
                node["pods"].remove(pod)
                node["evicting"] = False
            return 201, {"kind": "Eviction", "pod": pod}, None

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def parse_node(spec):
    try:
        name, rest = spec.split("=", 1)
        instance_id, pods = rest.split(":", 1)
        return name, instance_id, int(pods)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected NAME=INSTANCE_ID:PODS, got {spec!r}")


def main():
    parser = argparse.ArgumentParser(prog="python3 -m devcluster", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8701)
    parser.add_argument("--node", action="append", type=parse_node, default=[],
                        metavar="NAME=INSTANCE_ID:PODS")
    parser.add_argument("--token", action="append", default=[], metavar="TOKEN[=POD]",
                        help="accepted bearer token, optionally with the pod name it belongs to "
                             "(default: dev-token)")
    parser.add_argument("--evict-seconds", type=float, default=4.0)
    args = parser.parse_args()

    nodes = args.node or [("ip-10-0-1-17.ec2.internal", "i-0f1e2d3c4b5a69780", 10)]
    tokens = {}
    for spec in args.token or ["dev-token"]:
        token, _, pod = spec.partition("=")
        tokens[token] = pod or token
    cluster = Cluster(nodes, args.evict_seconds)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cluster, tokens))
    server.daemon_threads = True
    for name, instance_id, pods in nodes:
        print(f"node {name} instance {instance_id} pods {pods}", flush=True)
    print(f"cluster API on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
