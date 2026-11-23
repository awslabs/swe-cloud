"""Create the local stack with the same attributes as production (see docs/OPERATIONS.md).

    python3 tools/bootstrap.py          # idempotent; prints the env the processes read
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy

import boto3

VISIBILITY_TIMEOUT_S = 12
MAX_RECEIVE_COUNT = 5


def main():
    ddb = boto3.client("dynamodb")
    cfn = boto3.client("cloudformation")
    outputs = deploy(cfn, "notification-dev-queues",
                     Path(__file__).resolve().parents[1] / "infra/seed.template.json")
    dlq_url = outputs["DlqUrl"]
    urls = {"submission-queue": outputs["SubmissionUrl"], "reprocess-queue": outputs["ReprocessUrl"]}
    if "ReliabilityQueue" not in ddb.list_tables()["TableNames"]:
        deploy(cfn, "notification-dev-table",
               Path(__file__).resolve().parents[1] / "infra/table.template.json")

    print(f"export SUBMISSION_QUEUE_URL={urls['submission-queue']}")
    print(f"export REPROCESS_QUEUE_URL={urls['reprocess-queue']}")
    print(f"export DLQ_URL={dlq_url}")
    print("export TABLE_NAME=ReliabilityQueue")


if __name__ == "__main__":
    main()
