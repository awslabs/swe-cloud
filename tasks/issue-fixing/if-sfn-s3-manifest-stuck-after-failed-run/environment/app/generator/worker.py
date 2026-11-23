"""
The manifest generator, maintained by the data team. In production it runs as
a Lambda function; here it is a long-running process.

It consumes the generation tasks that the `Generate` state of the manifest
state machine sends to the task queue. For each task it looks up the files
selected by the filters in the catalog index, writes the manifest to the
bucket under the `object_key` given in the execution input, and only then
reports success to Step Functions, with `{"object_key": ..., "rows": ...}` as
the output. If the index can't be queried, it reports the failure instead
(error `IndexUnavailable`), which fails the execution.

    MANIFEST_TASK_QUEUE_URL, MANIFEST_BUCKET, CATALOG_INDEX_TABLE

    python3 -m generator.worker
"""
import csv
import io
import json
import logging
import os

import boto3
import botocore.config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
)

log = logging.getLogger('generator')

_client_config = botocore.config.Config(connect_timeout=5, read_timeout=10,
                                        retries={'mode': 'standard', 'max_attempts': 2})


class IndexUnavailable(Exception):
    pass


def select_files(dynamodb, table: str, catalog: str, filters: dict) -> list[dict]:
    try:
        paginator = dynamodb.get_paginator('query')
        pages = paginator.paginate(TableName=table,
                                   KeyConditionExpression='catalog = :c',
                                   ExpressionAttributeValues={':c': {'S': catalog}})
        items = [item for page in pages for item in page['Items']]
    except (ClientError, BotoCoreError) as e:
        raise IndexUnavailable(str(e)) from e
    files = [{k: next(iter(v.values())) for k, v in item.items()} for item in items]

    def selected(file):
        for field, filter in filters.items():
            (operator, values), = filter.items()
            value = file.get(field)
            if operator == 'is' and value not in values:
                return False
            if operator == 'within' and not any(lo <= float(value or 0) <= hi for lo, hi in values):
                return False
        return True

    return [file for file in files if selected(file)]


def render(format: str, files: list[dict]) -> bytes:
    if format == 'verbatim.jsonl':
        return ''.join(json.dumps(file, sort_keys=True) + '\n' for file in files).encode()
    else:
        columns = sorted({k for file in files for k in file} - {'catalog'})
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, delimiter='\t', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(files)
        return buffer.getvalue().encode()


def handle(task: dict, *, s3, sfn, dynamodb, bucket: str, table: str):
    input = task['input']
    try:
        files = select_files(dynamodb, table, input['catalog'], input['filters'])
    except IndexUnavailable as e:
        log.warning('Generation for %s failed: %s', task['execution_arn'], e)
        sfn.send_task_failure(taskToken=task['task_token'], error='IndexUnavailable', cause=str(e)[:256])
        return
    s3.put_object(Bucket=bucket, Key=input['object_key'], Body=render(input['format'], files))
    sfn.send_task_success(taskToken=task['task_token'],
                          output=json.dumps({'object_key': input['object_key'], 'rows': len(files)}))
    log.info('Generated %s with %d rows for %s', input['object_key'], len(files), task['execution_arn'])


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)7s %(name)s: %(message)s')
    logging.getLogger('botocore').setLevel(logging.WARNING)
    queue_url = os.environ['MANIFEST_TASK_QUEUE_URL']
    bucket = os.environ['MANIFEST_BUCKET']
    table = os.environ.get('CATALOG_INDEX_TABLE', 'catalog-index')
    s3, sfn, sqs, dynamodb = (boto3.client(s, config=_client_config)
                              for s in ('s3', 'stepfunctions', 'sqs', 'dynamodb'))
    log.info('Waiting for generation tasks on %s', queue_url)
    while True:
        response = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=10, MaxNumberOfMessages=10)
        for message in response.get('Messages', []):
            task = json.loads(message['Body'])
            try:
                handle(task, s3=s3, sfn=sfn, dynamodb=dynamodb, bucket=bucket, table=table)
            except Exception:
                log.exception('Could not handle task for %s', task.get('execution_arn'))
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message['ReceiptHandle'])


if __name__ == '__main__':
    main()
