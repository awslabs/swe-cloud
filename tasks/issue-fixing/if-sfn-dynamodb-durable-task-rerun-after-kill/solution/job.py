"""The task instance's job: one Standard Step Functions execution of the job state machine."""
import json
import logging
import time
import uuid

from durable_task import aws
from durable_task.ti import TaskInstance

log = logging.getLogger(__name__)

# Execution statuses, see the Step Functions DescribeExecution reference.
# A stopped run did not finish its work: it fails the try, and the next try starts the job again.
SUCCESS_STATUSES = ("SUCCEEDED",)
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

    def new_execution_arn(self) -> str:
        """ARN of a not-yet-started execution with a fresh name."""
        return self.execution_arn(uuid.uuid4().hex)

    def execution_arn(self, name: str) -> str:
        prefix = self.state_machine_arn.replace(":stateMachine:", ":execution:", 1)
        return f"{prefix}:{name}"

    def start(self, ti: TaskInstance, execution_arn: str) -> str:
        """Start the execution ``execution_arn`` names, or confirm it already exists.

        Standard StartExecution with the same name and input as a running execution returns that
        execution; a name that is closed (or was used with another input) is refused with
        ExecutionAlreadyExists. Either way the execution is the one ``execution_arn`` names.
        """
        name = execution_arn.rsplit(":", 1)[1]
        try:
            resp = self.sfn.start_execution(
                stateMachineArn=self.state_machine_arn, name=name, input=self.job_input(ti)
            )
        except self.sfn.exceptions.ExecutionAlreadyExists:
            log.info("execution %s already exists", execution_arn)
            return execution_arn
        log.info("started execution %s", resp["executionArn"])
        return resp["executionArn"]

    def status(self, execution_arn: str) -> str:
        try:
            return self.sfn.describe_execution(executionArn=execution_arn)["status"]
        except self.sfn.exceptions.ExecutionDoesNotExist:
            return NOT_FOUND

    def find_for_task_instance(self, ti: TaskInstance):
        """Return [(arn, status)] of every execution whose input names this task instance."""
        wanted = ti.identity()
        found = []
        for page in self.sfn.get_paginator("list_executions").paginate(
            stateMachineArn=self.state_machine_arn
        ):
            for ex in page.get("executions", []):
                try:
                    desc = self.sfn.describe_execution(executionArn=ex["executionArn"])
                except self.sfn.exceptions.ExecutionDoesNotExist:
                    continue
                if json.loads(desc.get("input") or "{}").get("task_instance") == wanted:
                    found.append((ex["executionArn"], desc["status"]))
        return found

    def find_unrecorded(self, ti: TaskInstance):
        """(arn, status) of a running or succeeded execution of this task instance, or None.

        Used where an earlier try may have started an execution without recording it. Listing is
        eventually consistent too, so an empty answer only counts once it has stayed empty for the
        visibility grace.
        """
        give_up = time.monotonic() + VISIBILITY_GRACE_S
        while True:
            found = self.find_for_task_instance(ti)
            for wanted in (self.is_active, self.is_succeeded):
                for arn, status in found:
                    if wanted(status):
                        return arn, status
            if time.monotonic() > give_up:
                return None
            time.sleep(self.poll_interval)

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
