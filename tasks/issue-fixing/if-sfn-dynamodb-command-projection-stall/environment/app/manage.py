"""Deployment and publisher CLI. These components are managed independently."""
import json
from pathlib import Path
import os
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from worker import client


def definition(queues):
    def task(queue, next_state, result_path=None):
        return {"Type": "Task", "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
                "Parameters": {"QueueUrl": queues[queue], "MessageBody": {
                    "entity.$": "$.entity", "version.$": "$.version",
                    "taskToken.$": "$$.Task.Token", "executionArn.$": "$$.Execution.Id"}},
                "ResultPath": result_path, "Next": next_state, "TimeoutSeconds": 120}
    states = {
        "route": {"Type": "Choice", "Choices": [{"Variable": "$.version", "NumericEquals": 1,
                                                    "Next": "sync_data"}], "Default": "wait_prev_command"},
        "wait_prev_command": task("wait-prev", "dup_check", "$.wait"),
        "dup_check": {"Type": "Choice", "Choices": [{"Variable": "$.wait.result", "StringEquals": "duplicate",
                                                        "Next": "duplicate_done"}], "Default": "sync_data"},
        "duplicate_done": {"Type": "Succeed"},
        "sync_data": task("sync", "finish"),
        "finish": task("finish", "done"),
        "done": {"Type": "Succeed"},
        "wait_prev_command_timeout": {"Type": "Pass", "Next": "failed"},
        "failed": {"Type": "Fail"}}
    states["wait_prev_command"]["Catch"] = [{"ErrorEquals": ["States.Timeout"], "Next": "wait_prev_command_timeout"}]
    return {"StartAt": "route", "States": states}


def init():
    ddb, sqs, iam, sfn = [client(s) for s in ("dynamodb", "sqs", "iam", "stepfunctions")]
    cfn = client("cloudformation")
    templates = Path(__file__).parent / "infra"
    for table, key_type in (("commands", "N"), ("effects", "S"), ("projection", None)):
        if table not in ddb.list_tables()["TableNames"]:
            if key_type is None:
                deploy(cfn, "seed-command-projection-table", templates / "projection.template.json")
            else:
                deploy(cfn, "seed-command-" + table + "-table", templates / "seed.template.json",
                       TableName=table, SortKeyType=key_type)
    outputs = deploy(cfn, "seed-command-queues", templates / "queues.template.json")
    queues = {"wait-prev": outputs["WaitQueue"], "sync": outputs["SyncQueue"], "finish": outputs["FinishQueue"]}
    try:
        role = iam.get_role(RoleName="command-role")["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role = deploy(cfn, "seed-command-role", templates / "role.template.json")["RoleArn"]
    machines = sfn.list_state_machines()["stateMachines"]
    arn = next((m["stateMachineArn"] for m in machines if m["name"] == "command-chain"), None)
    if not arn:
        arn = deploy(cfn, "seed-command-chain", templates / "machine.template.json",
                     RoleArn=role, Definition=json.dumps(definition(queues)))["MachineArn"]
    print(json.dumps({"queues": queues, "stateMachineArn": arn}, indent=2))


def publish(entity, version):
    ddb, sfn = client("dynamodb"), client("stepfunctions")
    ddb.update_item(TableName="commands", Key={"pk": {"S": entity}, "sk": {"N": str(version)}},
                    UpdateExpression="SET #s = if_not_exists(#s, :s)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": {"S": "created"}})
    arn = next(m["stateMachineArn"] for m in sfn.list_state_machines()["stateMachines"] if m["name"] == "command-chain")
    result = sfn.start_execution(stateMachineArn=arn, name=uuid.uuid4().hex,
                                 input=json.dumps({"entity": entity, "version": version}))
    print(result["executionArn"])


if __name__ == "__main__":
    if sys.argv[1:] == ["init"]:
        init()
    elif len(sys.argv) == 4 and sys.argv[1] == "publish":
        publish(sys.argv[2], int(sys.argv[3]))
    else:
        raise SystemExit("usage: manage.py init | publish ENTITY VERSION")
