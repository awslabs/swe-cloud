"""Provision an empty development deployment, printing connection arguments."""
import json
import os
from pathlib import Path
import time
import uuid
import boto3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from botocore.config import Config
import psycopg


def main():
    suffix = uuid.uuid4().hex[:10]
    endpoint = os.environ['AWS_ENDPOINT_URL']
    rds = boto3.client('rds', endpoint_url=endpoint, config=Config(read_timeout=600, retries={'max_attempts': 1}))
    name = 'wf-' + suffix
    deploy(boto3.client('cloudformation', endpoint_url=endpoint),
           'seed-' + name, Path(__file__).parent / 'infra/seed.template.json',
           InstanceName=name)
    while True:
        db = rds.describe_db_instances(DBInstanceIdentifier=name)['DBInstances'][0]
        if db['DBInstanceStatus'] == 'available':
            break
        time.sleep(1)
    host, port = db['Endpoint']['Address'], db['Endpoint']['Port']
    dsn = f'postgresql://syncadmin:local-password@{host}:{port}/sync'
    for _ in range(30):
        try:
            conn = psycopg.connect(dsn, autocommit=True, connect_timeout=2)
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else:
        raise RuntimeError('RDS endpoint unreachable')
    conn.execute(Path('/app/schema.sql').read_text())
    table = 'workflows-' + suffix
    deploy(boto3.client('cloudformation', endpoint_url=endpoint),
           'seed-' + table, Path(__file__).parent / 'infra/table.template.json',
           TableName=table, KeyName='wf_key')
    print(json.dumps({'pg_dsn': dsn, 'workflow_table': table, 'dynamodb_endpoint': endpoint}))


if __name__ == '__main__':
    main()
