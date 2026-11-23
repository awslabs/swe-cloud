"""
Create the resources of a local deployment on the emulator and seed the
catalog index. Idempotent. Prints the environment for the service instances
and the generator as shell `export` lines:

    eval "$(python3 deploy/bootstrap.py)"
"""
import json
import pathlib
import sys

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy

here = pathlib.Path(__file__).parent

machine_name = 'manifest-gen'
queue_name = 'manifest-gen-tasks'
bucket = 'manifests'
table = 'catalog-index'

trust_policy = {
    'Version': '2012-10-17',
    'Statement': [{
        'Effect': 'Allow',
        'Principal': {'Service': 'states.amazonaws.com'},
        'Action': 'sts:AssumeRole'
    }]
}

files = [
    ('dcp', 'f1', 'p-heart', 'heart', 'fastq.gz', 1200),
    ('dcp', 'f2', 'p-heart', 'heart', 'bam', 98000),
    ('dcp', 'f3', 'p-brain', 'brain', 'fastq.gz', 4300),
    ('dcp', 'f4', 'p-brain', 'brain', 'loom', 7600),
    ('dcp', 'f5', 'p-brain', 'cortex', 'fastq.gz', 5100),
    ('lungmap', 'f6', 'p-lung', 'lung', 'fastq.gz', 2500),
]


def log(*args):
    print(*args, file=sys.stderr)


def main():
    iam, sqs, s3, sfn, dynamodb = (boto3.client(s) for s in ('iam', 'sqs', 's3', 'stepfunctions', 'dynamodb'))

    outputs = deploy(boto3.client('cloudformation'), 'seed-' + machine_name,
                     here.parent / 'infra/seed.template.json',
                     QueueName=queue_name, BucketName=bucket, TableName=table)
    role_arn, queue_url = outputs['RoleArn'], outputs['QueueUrl']
    for catalog, file_id, project, organ, file_format, size in files:
        dynamodb.put_item(TableName=table, Item={
            'catalog': {'S': catalog},
            'file_id': {'S': file_id},
            'project': {'S': project},
            'organ': {'S': organ},
            'fileFormat': {'S': file_format},
            'fileSize': {'N': str(size)},
        })

    definition = (here / 'manifest-gen.asl.json').read_text().replace('${TaskQueueUrl}', queue_url)
    machine_arn = deploy(boto3.client('cloudformation'), 'workflow-' + machine_name,
                         here.parent / 'infra/workflow.template.json',
                         MachineName=machine_name, Definition=definition,
                         RoleArn=role_arn)['MachineArn']
    log('Deployed', machine_arn)
    for name, value in [('MANIFEST_STATE_MACHINE_ARN', machine_arn),
                        ('MANIFEST_BUCKET', bucket),
                        ('MANIFEST_TASK_QUEUE_URL', queue_url),
                        ('CATALOG_INDEX_TABLE', table)]:
        print(f'export {name}={value}')


if __name__ == '__main__':
    main()
