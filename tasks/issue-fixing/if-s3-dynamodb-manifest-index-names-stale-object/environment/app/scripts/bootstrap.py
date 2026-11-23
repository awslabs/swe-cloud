#!/usr/bin/env python3
"""Create the version-index table and the versioned warehouse bucket if they are missing.

    python3 /app/scripts/bootstrap.py

Uses AWS_ENDPOINT_URL, STRATA_TABLE and STRATA_BUCKET like stratactl does.
"""

import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from botocore.exceptions import ClientError  # noqa: E402

from strata import aws  # noqa: E402
from infra.cfn_deploy import deploy  # noqa: E402


def main():
    ddb = aws.dynamodb_client()
    table = aws.table_name()
    try:
        ddb.describe_table(TableName=table)
    except ClientError:
        cfn = boto3.client('cloudformation', endpoint_url=ddb.meta.endpoint_url,
                           region_name=ddb.meta.region_name)
        deploy(cfn, 'seed-table-' + table,
               Path(__file__).resolve().parents[1] / 'infra/seed.template.json', TableName=table)
    s3 = aws.s3_client()
    bucket = aws.bucket_name()
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        cfn = boto3.client('cloudformation', endpoint_url=s3.meta.endpoint_url,
                           region_name=s3.meta.region_name)
        deploy(cfn, 'seed-bucket-' + bucket,
               Path(__file__).resolve().parents[1] / 'infra/bucket.template.json', BucketName=bucket)
    else:
        # An existing developer bucket may be outside this script's CDK stack.
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    print(f"table {table} and bucket {bucket} (versioning enabled) are ready")


if __name__ == "__main__":
    main()
