"""Local development stack for stepq.

    python -m stepq.devstack up                 create (or reuse) the dev queue cluster, print VALKEY_URL
    python -m stepq.devstack runner [--port N]  a dev step runner: each step takes a random 200-3000 ms;
                                                runs are keyed by (item_id, lease_id) as in production
    python -m stepq.devstack enqueue [--count N] [--partition P] [--delay-ms D]
    python -m stepq.devstack status             dump items, ready queues and scavenger indexes

`up` talks to ElastiCache at AWS_ENDPOINT_URL; the other commands read VALKEY_URL.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis

from . import layout, ulid

DEV_CLUSTER = "stepq-dev"


def up() -> int:
    import boto3
    from botocore.config import Config

    ec = boto3.client("elasticache", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    try:
        ec.describe_cache_clusters(CacheClusterId=DEV_CLUSTER)
    except ec.exceptions.CacheClusterNotFoundFault:
        ec.create_cache_cluster(
            CacheClusterId=DEV_CLUSTER, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1
        )
    while True:
        cluster = ec.describe_cache_clusters(CacheClusterId=DEV_CLUSTER, ShowCacheNodeInfo=True)["CacheClusters"][0]
        if cluster["CacheClusterStatus"] == "available" and cluster.get("CacheNodes"):
            break
        time.sleep(1)
    endpoint = cluster["CacheNodes"][0]["Endpoint"]
    url = f"redis://{endpoint['Address']}:{endpoint['Port']}/0"
    for _ in range(30):
        try:
            redis.Redis.from_url(url).ping()
            break
        except redis.ConnectionError:
            time.sleep(1)
    print(f"VALKEY_URL={url}")
    return 0


def runner(port: int) -> int:
    runs: dict[tuple, dict] = {}
    lock = threading.Lock()

    def execute(run: dict) -> None:
        time.sleep(run["step_ms"] / 1000)
        run["status"] = 200
        run["done"].set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            pair = (body.get("item_id"), body.get("lease_id"))
            with lock:
                run = runs.get(pair)
                if run is None:
                    # A new pair starts a run; it carries on whether or not this request waits for it.
                    run = runs[pair] = {"step_ms": random.randint(200, 3000), "done": threading.Event(), "status": None}
                    print(f"{time.strftime('%H:%M:%S')} run {body} ({run['step_ms']} ms)", flush=True)
                    threading.Thread(target=execute, args=(run,), daemon=True).start()
                else:
                    print(f"{time.strftime('%H:%M:%S')} attach {body}", flush=True)
            run["done"].wait()
            out = json.dumps({"ok": run["status"] == 200}).encode()
            self.send_response(run["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(f"STEP_RUNNER_URL=http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
    return 0


def shard_now_ms(client: redis.Redis) -> int:
    """The shard's clock, which the API stamps run-at times from."""
    seconds, micros = client.time()
    return int(seconds) * 1000 + int(micros) // 1000


def enqueue(client: redis.Redis, partition: str, run_at_ms: int) -> str:
    """Add one item the way the API does: item JSON plus its ready-queue entry, atomically."""
    item_id = ulid.new()
    item = {"id": item_id, "partition": partition, "at": run_at_ms, "leaseID": None, "scavengeCount": 0}
    pipe = client.pipeline(transaction=True)
    pipe.hset(layout.ITEMS, item_id, json.dumps(item))
    pipe.zadd(layout.ready(partition), {item_id: run_at_ms})
    pipe.execute()
    return item_id


def status(client: redis.Redis) -> int:
    now = shard_now_ms(client)
    for item_id, raw in sorted(client.hgetall(layout.ITEMS).items()):
        item = json.loads(raw)
        lease = item.get("leaseID")
        left = f"{ulid.time_ms(lease) - now:+d} ms" if lease else "-"
        print(f"item {item_id} partition={item['partition']} lease={lease} expires_in={left} scavenged={item.get('scavengeCount')}")
    for key in client.scan_iter(match=layout.READY_PATTERN):
        print(f"ready {key}: {client.zrange(key, 0, -1, withscores=True)}")
    print(f"concurrency: {client.zrange(layout.CONCURRENCY, 0, -1, withscores=True)}")
    for partition in client.zrange(layout.CONCURRENCY, 0, -1):
        print(f"scavenger {partition}: {client.zrange(layout.scavenger(partition), 0, -1, withscores=True)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stepq.devstack")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up")
    run = sub.add_parser("runner")
    run.add_argument("--port", type=int, default=8700)
    enq = sub.add_parser("enqueue")
    enq.add_argument("--count", type=int, default=1)
    enq.add_argument("--partition", default=None)
    enq.add_argument("--delay-ms", type=int, default=0)
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.command == "up":
        return up()
    if args.command == "runner":
        return runner(args.port)
    client = redis.Redis.from_url(os.environ["VALKEY_URL"], decode_responses=True)
    if args.command == "status":
        return status(client)
    for _ in range(args.count):
        partition = args.partition or f"fn-{random.randbytes(4).hex()}"
        print(enqueue(client, partition, shard_now_ms(client) + args.delay_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
