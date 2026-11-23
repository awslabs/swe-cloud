"""Valkey key names, as gocraft/work lays them out (redis.go)."""


def _prefix(namespace: str) -> str:
    if namespace and not namespace.endswith(":"):
        namespace += ":"
    return namespace


def jobs(namespace: str, job_name: str) -> str:
    return f"{_prefix(namespace)}jobs:{job_name}"


def jobs_in_progress(namespace: str, pool_id: str, job_name: str) -> str:
    return f"{jobs(namespace, job_name)}:{pool_id}:inprogress"


def jobs_paused(namespace: str, job_name: str) -> str:
    return jobs(namespace, job_name) + ":paused"


def jobs_lock(namespace: str, job_name: str) -> str:
    return jobs(namespace, job_name) + ":lock"


def jobs_lock_info(namespace: str, job_name: str) -> str:
    return jobs(namespace, job_name) + ":lock_info"


def jobs_concurrency(namespace: str, job_name: str) -> str:
    return jobs(namespace, job_name) + ":max_concurrency"


def worker_pools(namespace: str) -> str:
    return _prefix(namespace) + "worker_pools"


def heartbeat(namespace: str, pool_id: str) -> str:
    return _prefix(namespace) + "worker_pools:" + pool_id


def jobs_runners(namespace: str, pool_id: str, job_name: str) -> str:
    """Not a gocraft/work key: pool <ID>'s claims whose runner is alive, raw JSON -> lease expiry (ms)."""
    return f"{jobs(namespace, job_name)}:{pool_id}:runners"
