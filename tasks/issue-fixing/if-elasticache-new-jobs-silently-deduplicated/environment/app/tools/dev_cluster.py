"""Create a throwaway ElastiCache (Valkey) node on the local AWS endpoint and print its URL.

    eval "$(python3 tools/dev_cluster.py)"      # exports DQ_REDIS_URL

Each call creates a new node (names must be unique), so you get an empty keyspace.
"""

import sys
import time
import uuid

import boto3
import redis
from botocore.config import Config


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else f"dq-dev-{uuid.uuid4().hex[:8]}"
    # create_cache_cluster blocks until the node is up; don't let botocore retry it.
    ec = boto3.client("elasticache", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    ec.create_cache_cluster(CacheClusterId=name, Engine="valkey", CacheNodeType="cache.t3.micro",
                            NumCacheNodes=1)
    for _ in range(300):
        cluster = ec.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)["CacheClusters"][0]
        if cluster["CacheClusterStatus"] == "available" and cluster.get("CacheNodes"):
            endpoint = cluster["CacheNodes"][0]["Endpoint"]
            url = f"redis://{endpoint['Address']}:{endpoint['Port']}/0"
            # The endpoint takes a few seconds to become reachable after "available".
            for _ in range(60):
                try:
                    redis.Redis.from_url(url, socket_connect_timeout=2).ping()
                    break
                except redis.ConnectionError:
                    time.sleep(0.5)
            print(f"export DQ_REDIS_URL={url}")
            return
        time.sleep(1)
    raise SystemExit(f"cache cluster {name} did not become available")


if __name__ == "__main__":
    main()
