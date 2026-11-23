"""One try of a task instance whose work is a long-running external job."""
import logging

from durable_task.job import JobClient, JobRunFailed
from durable_task.store import TaskStateStore
from durable_task.ti import TaskInstance

log = logging.getLogger(__name__)


class ResumableJob:
    """Run a task instance's job so that a later try picks up where an earlier one left off.

    The ARN of the execution the task instance is running is kept in the task state store, which
    outlives the worker. On a later try the stored execution's status decides what happens:

    - still running: reconnect to it and keep polling;
    - succeeded: return straight away without starting anything;
    - anything else: start the job again.
    """

    # Attribute of the task state store record that holds the execution ARN. Other components
    # read it (docs/state-store.md); renaming it breaks tries already in flight.
    external_id_key = "execution_arn"

    def __init__(self, ti: TaskInstance, store: TaskStateStore, job: JobClient):
        self.ti = ti
        self.store = store
        self.job = job

    def execute(self) -> str:
        stored = self.store.get(self.external_id_key)
        if stored:
            status = self.job.status(stored)
            if self.job.is_active(status):
                log.info("reconnecting to %s (%s)", stored, status)
                return self._finish(stored)
            if self.job.is_succeeded(status):
                log.info("%s already finished (%s); nothing to start", stored, status)
                return status
            log.warning("previous execution %s is %s; starting the job again", stored, status)
        else:
            log.info("no execution recorded for %s; starting the job", self.ti)

        execution_arn = self._submit(has_record=bool(stored))
        self.store.set(self.external_id_key, execution_arn)
        return self._finish(execution_arn)

    def _submit(self, has_record: bool) -> str:
        # Task instances that ran before the state store existed have no record. On a retry, look
        # for an execution this task instance already started before starting another one.
        if self.ti.try_number > 1 and not has_record:
            found = self.job.find_for_task_instance(self.ti)
            if found:
                arn, status = found
                log.info("found earlier execution %s (%s)", arn, status)
                if self.job.is_active(status):
                    return arn
        return self.job.start(self.ti)

    def _finish(self, execution_arn: str) -> str:
        status = self.job.wait(execution_arn)
        if not self.job.is_succeeded(status):
            raise JobRunFailed(f"execution {execution_arn} ended {status}")
        log.info("execution %s ended %s", execution_arn, status)
        return status
