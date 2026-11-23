"""The fixed Valkey key layout. The batch fleet and the ops tooling read and write the same keys."""

PREFIX = "{q}"
ITEMS = f"{PREFIX}:queue:item"            # HASH  item id -> item JSON
CONCURRENCY = f"{PREFIX}:concurrency"     # ZSET  partition -> earliest lease expiry (ms) in it
READY_PATTERN = f"{PREFIX}:queue:sorted:*"


def ready(partition: str) -> str:
    """ZSET of due/queued item ids in a partition, score = run-at (ms)."""
    return f"{PREFIX}:queue:sorted:{partition}"


def scavenger(partition: str) -> str:
    """ZSET of leased item ids in a partition, score = lease expiry (ms)."""
    return f"{PREFIX}:scavenger:{partition}"


def partition_of_ready_key(key: str) -> str:
    return key[len(f"{PREFIX}:queue:sorted:"):]


def lease_keys(partition: str) -> list[str]:
    return [ITEMS, CONCURRENCY, ready(partition), scavenger(partition)]


def extend_keys(partition: str) -> list[str]:
    return [ITEMS, CONCURRENCY, scavenger(partition)]


requeue_keys = lease_keys
scavenge_keys = lease_keys
dequeue_keys = extend_keys
