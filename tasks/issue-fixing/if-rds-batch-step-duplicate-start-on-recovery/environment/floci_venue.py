#!/usr/bin/env python3
"""floci-venue: make the engines Floci starts reachable from the task container, at the endpoints Floci reports.

Floci runs in the ``engines`` service and starts each engine (Postgres, MySQL, Valkey, Redpanda,
OpenSearch) as a container on that service's private Docker daemon, which this container cannot
reach. Three gaps remain between what Floci reports and what this container can connect to:

- engine containers sit on the daemon's bridge (``ENGINE_NET``), routable only through ``engines``;
- an endpoint reported as ``localhost`` is a port on ``engines``, not on this container;
- a Kafka broker advertises its container name, which only the daemon's own DNS resolves.

``floci-venue up`` closes them and is idempotent: it adds the route, waits for Floci, and keeps one
watcher running that forwards every loopback endpoint to ``engines`` and maps every advertised
broker name to its address. Once it returns, code connects to whatever Floci reports.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ENGINES = "engines"
ENGINE_NET = "10.89.0.0/16"
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", f"http://{ENGINES}:4566")
PIDFILE = Path("/run/floci-venue.pid")
LOG = Path("/tmp/floci-venue.log")
HOSTS = Path("/etc/hosts")
_POLL_S = 1.0


def _log(message: str) -> None:
    sys.stdout.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    sys.stdout.flush()


def _loopback(host: str) -> bool:
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolves(host: str) -> bool:
    try:
        socket.gethostbyname(host)
    except OSError:
        return False
    return True


# ── route and health ───────────────────────────────────────────────────────────────


def route() -> str:
    """Send the engine bridge through ``engines``; the only route this container has to an engine."""
    via = socket.gethostbyname(ENGINES)
    subprocess.run(["ip", "route", "replace", ENGINE_NET, "via", via], check=True, capture_output=True, text=True)
    return via


def healthy(timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ValueError), urllib.request.urlopen(f"{ENDPOINT}/_floci/health", timeout=2) as response:
            if response.status == 200:
                return True
        time.sleep(1)
    return False


# ── the watcher ────────────────────────────────────────────────────────────────────


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while chunk := source.recv(65536):
            sink.sendall(chunk)
    for sock in (source, sink):
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def _forward(listener: socket.socket, port: int) -> None:
    while True:
        client, _ = listener.accept()
        try:
            upstream = socket.create_connection((ENGINES, port), timeout=10)
            upstream.settimeout(None)
        except OSError:
            client.close()
            continue
        for source, sink in ((client, upstream), (upstream, client)):
            threading.Thread(target=_pipe, args=(source, sink), daemon=True).start()


def _kafka_brokers(host: str, port: int) -> list[tuple[str, int]]:
    """The brokers a Kafka bootstrap advertises, by a Metadata v0 request: the standard library is enough."""
    client = b"floci-venue"
    body = struct.pack(">hhih", 3, 0, 1, len(client)) + client + struct.pack(">i", 0)
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(struct.pack(">i", len(body)) + body)
        size = struct.unpack(">i", _exactly(sock, 4))[0]
        data = _exactly(sock, size)
    offset = 4  # correlation id
    (count,) = struct.unpack_from(">i", data, offset)
    offset += 4
    brokers = []
    for _ in range(count):
        _node, length = struct.unpack_from(">ih", data, offset)
        offset += 6
        name = data[offset : offset + length].decode()
        offset += length
        (broker_port,) = struct.unpack_from(">i", data, offset)
        offset += 4
        brokers.append((name, broker_port))
    return brokers


def _exactly(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("connection closed")
        data += chunk
    return data


class Watcher:
    """Every endpoint Floci reports, made reachable: loopback ports forwarded, broker names in /etc/hosts."""

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        config = Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1})
        self.clients = {name: boto3.client(name, endpoint_url=ENDPOINT, config=config) for name in ("rds", "elasticache", "kafka", "opensearch")}
        self.forwarded: set[int] = set()
        self.mapped: set[str] = set()
        self.bootstraps: set[str] = set()

    def endpoints(self) -> list[tuple[str, int]]:
        """Every host:port Floci reports for an engine; a service that answers with an error has none yet."""
        found: list[tuple[str, int]] = []

        def take(endpoint: object) -> None:
            if isinstance(endpoint, dict) and endpoint.get("Address") and endpoint.get("Port"):
                found.append((str(endpoint["Address"]), int(endpoint["Port"])))

        with contextlib.suppress(Exception):
            for instance in self.clients["rds"].describe_db_instances()["DBInstances"]:
                take(instance.get("Endpoint"))
        with contextlib.suppress(Exception):
            for cluster in self.clients["rds"].describe_db_clusters()["DBClusters"]:
                found.extend((cluster[key], int(cluster.get("Port") or 0)) for key in ("Endpoint", "ReaderEndpoint") if cluster.get(key))
        with contextlib.suppress(Exception):
            for cluster in self.clients["elasticache"].describe_cache_clusters(ShowCacheNodeInfo=True)["CacheClusters"]:
                take(cluster.get("ConfigurationEndpoint"))
                for node in cluster.get("CacheNodes") or ():
                    take(node.get("Endpoint"))
        with contextlib.suppress(Exception):
            for group in self.clients["elasticache"].describe_replication_groups()["ReplicationGroups"]:
                take(group.get("ConfigurationEndpoint"))
                for member in group.get("NodeGroups") or ():
                    take(member.get("PrimaryEndpoint"))
                    take(member.get("ReaderEndpoint"))
        with contextlib.suppress(Exception):
            names = [d["DomainName"] for d in self.clients["opensearch"].list_domain_names()["DomainNames"]]
            for domain in self.clients["opensearch"].describe_domains(DomainNames=names)["DomainStatusList"] if names else ():
                scheme, _, address = (domain.get("Endpoint") or "").rpartition("://")
                host, _, port = address.partition(":")
                if host:
                    found.append((host, int(port or (443 if scheme == "https" else 80))))
        with contextlib.suppress(Exception):
            for cluster in self.clients["kafka"].list_clusters_v2()["ClusterInfoList"]:
                with contextlib.suppress(Exception):
                    brokers = self.clients["kafka"].get_bootstrap_brokers(ClusterArn=cluster["ClusterArn"])
                    self.bootstraps.update(filter(None, (brokers.get("BootstrapBrokerString") or "").split(",")))
        return [(host, port) for host, port in found if port]

    def forward(self, port: int) -> None:
        if port in self.forwarded:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            _log(f"cannot forward 127.0.0.1:{port}: {error}")
            listener.close()
            self.forwarded.add(port)
            return
        listener.listen(128)
        threading.Thread(target=_forward, args=(listener, port), daemon=True).start()
        self.forwarded.add(port)
        _log(f"forwarding 127.0.0.1:{port} -> {ENGINES}:{port}")

    def map(self, name: str, address: str) -> None:
        if name in self.mapped or _resolves(name):
            self.mapped.add(name)
            return
        with HOSTS.open("a") as hosts:
            hosts.write(f"{address} {name}\n")
        self.mapped.add(name)
        _log(f"mapped {name} -> {address}")

    def once(self) -> None:
        for host, port in self.endpoints():
            if _loopback(host):
                self.forward(port)
            elif not _resolves(host):
                self.map(host, socket.gethostbyname(ENGINES))
        for entry in list(self.bootstraps):
            host, _, port = entry.rpartition(":")
            if _loopback(host):
                self.forward(int(port))
                host = ENGINES
            with contextlib.suppress(OSError, struct.error, UnicodeDecodeError):
                address = socket.gethostbyname(host)
                for name, _broker_port in _kafka_brokers(host, int(port)):
                    if _loopback(name):
                        self.forward(_broker_port)
                    else:
                        self.map(name, address)
                self.bootstraps.discard(entry)

    def run(self) -> None:
        while True:
            try:
                self.once()
            except Exception as error:  # the watcher outlives any one bad answer
                _log(f"watch: {type(error).__name__}: {error}")
            time.sleep(_POLL_S)


# ── commands ───────────────────────────────────────────────────────────────────────


def _running() -> bool:
    with contextlib.suppress(OSError, ValueError):
        pid = int(PIDFILE.read_text())
        os.kill(pid, 0)
        return "watch" in Path(f"/proc/{pid}/cmdline").read_text()
    return False


def up(timeout_s: float = 300) -> int:
    via = route()
    if not healthy(timeout_s):
        sys.stderr.write(f"floci-venue: Floci at {ENDPOINT} did not become healthy in {timeout_s:.0f} s\n")
        return 1
    if not _running():
        with LOG.open("a") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        PIDFILE.write_text(str(process.pid))
    sys.stdout.write(json.dumps({"engines": via, "route": ENGINE_NET, "endpoint": ENDPOINT, "watcher": True}) + "\n")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "up"
    if command == "up":
        return up()
    if command == "watch":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        Watcher().run()
        return 0
    sys.stderr.write("usage: floci-venue [up|watch]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
