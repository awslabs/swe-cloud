"""Create the local resources the worker needs on the emulator (idempotent).

    python3 /app/infra/bootstrap.py

Creates the task state store table, the job's state machine (from job-definition.json) with its
execution role, and the worker role with worker-policy.json, then prints the state machine ARN.
"""
import json
import pathlib

import boto3
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from cdk_infra.cfn_deploy import deploy

HERE = pathlib.Path(__file__).resolve().parent
TABLE = "task_state_store"
STATE_MACHINE = "etl-load-job"
STATES_ROLE = "etl-load-job-execution"
WORKER_ROLE = "durable-task-worker"


def _role(iam, name: str, service: str) -> str:
    return deploy(boto3.client("cloudformation"), "seed-" + name,
                  HERE.parent / "cdk_infra/role.template.json",
                  RoleName=name, Service=service)["RoleArn"]


def main() -> None:
    iam = boto3.client("iam")
    ddb = boto3.client("dynamodb")
    sfn = boto3.client("stepfunctions")

    try:
        deploy(boto3.client("cloudformation"), "seed-task-state-store",
               HERE.parent / "cdk_infra/seed.template.json", TableName=TABLE)
    except ddb.exceptions.ResourceInUseException:
        pass

    states_role = _role(iam, STATES_ROLE, "states.amazonaws.com")
    _role(iam, WORKER_ROLE, "ec2.amazonaws.com")
    policy = (HERE / "worker-policy.json").read_text()
    policy = policy.replace("${JobStateMachine}", STATE_MACHINE).replace("${TaskStateStoreTable}", TABLE)
    deploy(boto3.client("cloudformation"), "seed-worker-policy",
           HERE.parent / "cdk_infra/policy.template.json",
           RoleName=WORKER_ROLE, PolicyDocument=policy)

    definition = (HERE / "job-definition.json").read_text()
    arn = None
    for page in sfn.get_paginator("list_state_machines").paginate():
        for sm in page["stateMachines"]:
            if sm["name"] == STATE_MACHINE:
                arn = sm["stateMachineArn"]
    if arn is None:
        arn = deploy(boto3.client("cloudformation"), "seed-" + STATE_MACHINE,
                     HERE.parent / "cdk_infra/machine.template.json",
                     MachineName=STATE_MACHINE, Definition=definition,
                     RoleArn=states_role)["MachineArn"]
    print(f"table:         {TABLE}")
    print(f"state machine: {arn}")


if __name__ == "__main__":
    main()
