"""Provision an ordinary development instance, printing environment assignments."""
import json
import os
import time
import uuid

import boto3
import requests
from botocore.config import Config


def provision():
    suffix = uuid.uuid4().hex[:10]
    client = boto3.client('opensearch', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                          region_name='us-east-1',
                          config=Config(read_timeout=600, retries={'max_attempts': 1}))
    name = 'sink-' + suffix
    client.create_domain(DomainName=name, EngineVersion='OpenSearch_2.11',
                         ClusterConfig={'InstanceType': 't3.small.search', 'InstanceCount': 1})
    for _ in range(180):
        status = client.describe_domain(DomainName=name)['DomainStatus']
        endpoint = status.get('Endpoint')
        if endpoint and not status.get('Processing', False):
            break
        time.sleep(1)
    else:
        raise RuntimeError('domain not available')
    endpoint = endpoint if endpoint.startswith('http') else 'http://' + endpoint
    session = requests.Session()
    session.trust_env = False
    for _ in range(90):
        try:
            session.get(endpoint, timeout=2).raise_for_status()
            break
        except requests.RequestException:
            time.sleep(1)
    else:
        raise RuntimeError('domain endpoint not reachable')
    template = {'index_patterns': ['events-*'], 'template': {
        'settings': {'number_of_shards': 1, 'number_of_replicas': 0, 'refresh_interval': '5s'},
        'mappings': {'dynamic': 'strict', 'properties': {
            'key': {'type': 'keyword'}, 'ts': {'type': 'date'},
            'payload': {'type': 'keyword'}, 'potential_duplicate': {'type': 'boolean'}}}}}
    session.put(endpoint + '/_index_template/events', json=template, timeout=15).raise_for_status()
    session.put(endpoint + '/events-000001', json={'aliases': {'events': {'is_write_index': True}}},
                timeout=15).raise_for_status()
    bucket = 'sink-dlq-' + suffix
    boto3.client('s3', endpoint_url=os.environ['AWS_ENDPOINT_URL'], region_name='us-east-1').create_bucket(Bucket=bucket)
    s3 = boto3.client('s3')
    for number in range(32):
        s3.put_object(Bucket=bucket, Key='inbox/catalog/%02d.json' % number,
                      Body=b'{"entries":{}}', ContentType='application/json')
    return {'OS_ENDPOINT': endpoint, 'WRITE_ALIAS': 'events', 'QUERY_TERM': 'key',
            'DLQ_BUCKET': bucket, 'DLQ_PREFIX': 'dlq/',
            'INBOX_BUCKET': bucket, 'INBOX_PREFIX': 'inbox/', 'MAX_LATE_APPLY_S': '8',
            'WORKER_ID': 'worker-0', 'INTAKE_PORT': '8080'}


if __name__ == '__main__':
    for key, value in provision().items():
        print(f'export {key}={value}')
