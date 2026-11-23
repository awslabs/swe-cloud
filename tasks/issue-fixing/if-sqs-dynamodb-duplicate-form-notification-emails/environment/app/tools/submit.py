"""Submit a test form response the way the forms API does.

The forms API (a separate service, not deployed from this repository) writes the
ReliabilityQueue item first and then enqueues a pointer to it. This script writes exactly the
same item and message, so the worker sees what it sees in production.

    python3 tools/submit.py --email owner@example.gc.ca [--count 1]
"""
import argparse
import json
import os
import time
import uuid

import boto3


def submit(email, form_id="clf2d6x0k0000qs0z8l8m5r1z", language="en"):
    sqs = boto3.client("sqs")
    ddb = boto3.client("dynamodb")
    table = os.environ.get("TABLE_NAME", "ReliabilityQueue")
    queue_url = os.environ.get("SUBMISSION_QUEUE_URL") or sqs.get_queue_url(
        QueueName="submission-queue"
    )["QueueUrl"]

    submission_id = str(uuid.uuid4())
    form_data = {
        "formID": form_id,
        "language": language,
        "securityAttribute": "Protected A",
        "responses": {"1": "Jane Doe", "2": "Renewal of permit 44-1170", "3": "Yes"},
        "deliveryOption": {"emailAddress": email, "emailSubjectEn": "Permit renewal request"},
        "form": {"titleEn": "Permit renewal", "titleFr": "Renouvellement de permis"},
    }
    ddb.put_item(
        TableName=table,
        Item={
            "SubmissionID": {"S": submission_id},
            "FormID": {"S": form_id},
            "SendReceipt": {"S": "unknown"},
            "FormSubmissionLanguage": {"S": language},
            "FormData": {"S": json.dumps(form_data)},
            "CreatedAt": {"N": str(int(time.time() * 1000))},
            "SecurityAttribute": {"S": "Protected A"},
            "NotifyState": {"S": "PENDING"},
        },
    )
    receipt = sqs.send_message(
        QueueUrl=queue_url, MessageBody=json.dumps({"submissionID": submission_id})
    )["MessageId"]
    ddb.update_item(
        TableName=table,
        Key={"SubmissionID": {"S": submission_id}},
        UpdateExpression="SET SendReceipt = :receipt",
        ExpressionAttributeValues={":receipt": {"S": receipt}},
    )
    return submission_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="forms-owner@example.gc.ca")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()
    for _ in range(args.count):
        print(submit(args.email))


if __name__ == "__main__":
    main()
