import os

import redis


def connect(url=None):
    """Connect to the queue's Valkey node (ElastiCache) named by DQ_REDIS_URL."""
    url = url or os.environ.get("DQ_REDIS_URL")
    if not url:
        raise SystemExit("DQ_REDIS_URL is not set (redis://<host>:<port>/0)")
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=30,
                                socket_connect_timeout=10, health_check_interval=30)
