"""The task instance's job: one Standard Step Functions execution of the job state machine."""
import json
import logging
import time
import uuid

from durable_task import aws
from durable_task.ti import TaskInstance

log = logging.getLogger(__name__)

# Execution statuses, see the Step Functions DescribeExecution reference.
# A run someone stopped on purpose has finished: starting it again would undo the stop, so a
# stopped run counts as done, like a succeeded one.
SUCCESS_STATUSES = ("SUCCEEDED", "ABORTED")
TERMINAL_STATUSES = ("SUCCEEDED", "ABORTED", "FAILED", "TIMED_OUT")
# Synthetic status for an execution the service does not know about.
NOT_FOUND = "NOT_FOUND"

POLL_INTERVAL_S = 1.0
VISIBILITY_GRACE_S = 30.0


class JobRunFailed(Exception):
    pass


class JobClient:
    def __init__(self, state_machine_arn: str, client=None, poll_interval: float = POLL_INTERVAL_S):
        self.state_machine_arn = state_machine_arn
        self.sfn = client or aws.client("stepfunctions")
        self.poll_interval = poll_interval

    # -- classification -------------------------------------------------------------------------
    def is_active(self, status: str) -> bool:
        return status not in (*TERMINAL_STATUSES, NOT_FOUND)

    def is_succeeded(self, status: str) -> bool:
        return status in SUCCESS_STATUSES

    # -- service calls --------------------------------------------------------------------------
    def job_input(self, ti: TaskInstance) -> str:
        """Execution input. The job reads ``task_instance`` to know which partition to load."""
        return json.dumps({"task_instance": ti.identity()}, sort_keys=True, separators=(",", ":"))

    def start(self, ti: TaskInstance) -> str:
        resp = self.sfn.start_execution(
            stateMachineArn=self.state_machine_arn,
            name=uuid.uuid4().hex,
            input=self.job_input(ti),
        )
        log.info("started execution %s", resp["executionArn"])
        return resp["executionArn"]

    def status(self, execution_arn: str) -> str:
        try:
            return self.sfn.describe_execution(executionArn=execution_arn)["status"]
        except self.sfn.exceptions.ExecutionDoesNotExist:
            return NOT_FOUND

    def find_for_task_instance(self, ti: TaskInstance):
        """Return (arn, status) of the first execution whose input names this task instance."""
        wanted = ti.identity()
        for page in self.sfn.get_paginator("list_executions").paginate(
            stateMachineArn=self.state_machine_arn
        ):
            for ex in page.get("executions", []):
                try:
                    desc = self.sfn.describe_execution(executionArn=ex["executionArn"])
                    if json.loads(desc.get("input") or "{}").get("task_instance") == wanted:
                        return ex["executionArn"], desc["status"]
                except Exception:  # noqa: BLE001 - best effort lookup
                    log.warning("could not inspect %s", ex.get("executionArn"), exc_info=True)
        return None

    def wait(self, execution_arn: str) -> str:
        """Poll until the execution reaches a terminal status and return it."""
        visible_by = time.monotonic() + VISIBILITY_GRACE_S
        last = None
        while True:
            status = self.status(execution_arn)
            if status != last:
                log.info("execution %s is %s", execution_arn, status)
                last = status
            if status == NOT_FOUND:
                # DescribeExecution is eventually consistent: an execution started moments ago
                # can be missing for a short while. Give it time to show up before calling it gone.
                if time.monotonic() > visible_by:
                    raise JobRunFailed(f"execution {execution_arn} does not exist")
            elif status in TERMINAL_STATUSES:
                return status
            time.sleep(self.poll_interval)
