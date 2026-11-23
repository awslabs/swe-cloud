"""Deploy a CDK-synthesized CloudFormation template and return the stack's outputs."""
import json
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def deploy(cfn, stack_name, template, **parameters):
    body = Path(template).read_text()
    params = [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in parameters.items()]
    try:
        cfn.create_stack(StackName=stack_name, TemplateBody=body, Parameters=params)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "AlreadyExistsException":
            raise
        try:
            cfn.update_stack(StackName=stack_name, TemplateBody=body, Parameters=params)
        except ClientError as update_exc:
            if "No updates" not in str(update_exc):
                raise
    deadline = time.monotonic() + 600
    while True:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
        if not stack["StackStatus"].endswith("IN_PROGRESS") or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise RuntimeError(f"{stack_name}: {stack['StackStatus']}: {stack.get('StackStatusReason')}")
    _match_billing_mode(cfn, stack_name, json.loads(body))
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _match_billing_mode(cfn, stack_name, template):
    # The emulator's CloudFormation records a PAY_PER_REQUEST table as provisioned 5/5.
    ddb = boto3.client("dynamodb", endpoint_url=cfn.meta.endpoint_url, region_name=cfn.meta.region_name)
    for resource in cfn.describe_stack_resources(StackName=stack_name)["StackResources"]:
        declared = template["Resources"].get(resource["LogicalResourceId"], {})
        if declared.get("Type") == "AWS::DynamoDB::Table" and declared["Properties"].get("BillingMode") == "PAY_PER_REQUEST":
            table = ddb.describe_table(TableName=resource["PhysicalResourceId"])["Table"]
            if "BillingModeSummary" not in table:
                ddb.update_table(TableName=resource["PhysicalResourceId"], BillingMode="PAY_PER_REQUEST")
