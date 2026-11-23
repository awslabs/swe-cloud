"""One try of a task instance whose work is a long-running external job."""
import logging

from durable_task.job import NOT_FOUND, JobClient, JobRunFailed
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
    # Set on records whose execution_arn was written before the execution was started. Records
    # without it come from the previous version, which recorded a start only after it returned.
    written_ahead_key = "execution_arn_written_ahead"

    def __init__(self, ti: TaskInstance, store: TaskStateStore, job: JobClient):
        self.ti = ti
        self.store = store
        self.job = job

    def execute(self) -> str:
        item = self.store.get_all()
        stored = item.get(self.external_id_key)
        if stored:
            status = self.job.status(stored)
            if status == NOT_FOUND and item.get(self.written_ahead_key):
                # Recorded before it was started: either the try that recorded it died before the
                # start reached Step Functions, or it was started and is not visible yet
                # (DescribeExecution is eventually consistent). Starting the same name with the
                # same input is right in both cases.
                log.info("recorded execution %s not visible; starting it under its own name", stored)
                return self._start_recorded(stored)
            if status == NOT_FOUND or self.job.is_active(status):
                log.info("reconnecting to %s (%s)", stored, status)
                return self._finish(stored)
            if self.job.is_succeeded(status):
                log.info("%s already finished (%s); nothing to start", stored, status)
                return status
            log.warning("previous execution %s is %s", stored, status)
            if not item.get(self.written_ahead_key) and int(item.get("try_number", 0)) < self.ti.try_number - 1:
                # The previous version started first and recorded after. A try after the one that
                # wrote this record may have started an execution and died before recording it.
                found = self.job.find_unrecorded(self.ti)
                if found:
                    return self._adopt(*found)
        elif self.ti.try_number > 1:
            # Either earlier tries never got as far as the job, or one of the previous version
            # started it and died before recording it.
            found = self.job.find_unrecorded(self.ti)
            if found:
                return self._adopt(*found)

        # Record the execution before starting it, so whatever kills this try, the record (and so
        # the next try and the reconciler) already names the only execution that can be running.
        execution_arn = self.job.new_execution_arn()
        self.store.update({self.external_id_key: execution_arn, self.written_ahead_key: True})
        return self._start_recorded(execution_arn)

    def _adopt(self, execution_arn: str, status: str) -> str:
        log.info("found unrecorded execution %s (%s)", execution_arn, status)
        self.store.update({self.external_id_key: execution_arn, self.written_ahead_key: True})
        if self.job.is_succeeded(status):
            return status
        return self._finish(execution_arn)

    def _start_recorded(self, execution_arn: str) -> str:
        started = self.job.start(self.ti, execution_arn)
        if started != execution_arn:
            self.store.set(self.external_id_key, started)
        return self._finish(started)

    def _finish(self, execution_arn: str) -> str:
        status = self.job.wait(execution_arn)
        if not self.job.is_succeeded(status):
            raise JobRunFailed(f"execution {execution_arn} ended {status}")
        log.info("execution %s ended %s", execution_arn, status)
        return status
