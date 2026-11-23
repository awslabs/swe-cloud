import json
import time

from .keys import QueueKeys
from .scripts import Scripts


def now_ms():
    return int(time.time() * 1000)


class Queue:
    def __init__(self, conn, name):
        self.conn = conn
        self.name = name
        self.keys = QueueKeys(name)
        self.scripts = Scripts(conn)

    def add(self, job_name, data, dedup_id=None, ttl_ms=None, keep_last=False):
        """Add a job. Returns (job_id, deduplicated).

        With a dedup_id, at most one job per id is in flight: when the add is
        deduplicated no job is created and job_id is the job that already
        holds the id. ttl_ms turns the id into a throttle window instead;
        keep_last keeps the data of an add made while that job runs, for a
        job created once it finishes (see README.md, "Deduplication").
        """
        k = self.keys
        created, job_id = self.scripts.addJob(
            keys=[k.id, k.wait, k.events],
            args=[k.prefix, job_name, json.dumps(data), dedup_id or "", int(ttl_ms or 0), now_ms(),
                  1 if keep_last else 0],
        )
        return str(job_id), not int(created)

    def get_job(self, job_id):
        return self.conn.hgetall(self.keys.job(job_id)) or None

    def dedup_owner(self, dedup_id):
        return self.conn.get(self.keys.dedup(dedup_id))

    def dedup_next(self, dedup_id):
        return self.conn.hgetall(self.keys.dedup_next(dedup_id)) or None
