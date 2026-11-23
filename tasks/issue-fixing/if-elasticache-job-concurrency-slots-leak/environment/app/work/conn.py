"""The Valkey client a pool shares between its workers, heartbeater and reaper."""

import redis

from .config import Config


def connect(config: Config) -> redis.Redis:
    # One client per pool process; redis-py hands each command a connection from the pool and
    # opens a new one on the next command after a connection is lost.
    return redis.Redis(
        host=config.valkey_host,
        port=config.valkey_port,
        socket_connect_timeout=5,
        socket_timeout=30,
        health_check_interval=0,
    )
