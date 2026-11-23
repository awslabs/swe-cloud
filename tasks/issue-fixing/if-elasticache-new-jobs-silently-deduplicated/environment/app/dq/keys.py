"""Key layout of a queue. Ops tooling and dashboards read these names; keep them stable."""


class QueueKeys:
    def __init__(self, queue):
        self.queue = queue
        self.prefix = f"dq:{queue}:"
        self.id = self.prefix + "id"
        self.wait = self.prefix + "wait"
        self.active = self.prefix + "active"
        self.delayed = self.prefix + "delayed"
        self.completed = self.prefix + "completed"
        self.failed = self.prefix + "failed"
        self.stalled = self.prefix + "stalled"
        self.stalled_check = self.prefix + "stalled-check"
        self.events = self.prefix + "events"

    def job(self, job_id):
        return f"{self.prefix}job:{job_id}"

    def lock(self, job_id):
        return f"{self.prefix}job:{job_id}:lock"

    def dedup(self, dedup_id):
        return f"{self.prefix}de:{dedup_id}"

    def dedup_next(self, dedup_id):
        return f"{self.prefix}dn:{dedup_id}"
