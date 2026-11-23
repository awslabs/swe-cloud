"""Provision a development MSK broker: python3 /app/provision.py."""
import json
import os
import socket
import time
import uuid
import boto3
from botocore.config import Config
from kwire import create_topic


def provision():
    client = boto3.client('kafka', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                          config=Config(read_timeout=600, retries={'max_attempts': 1}))
    result = client.create_cluster(ClusterName='audit-' + uuid.uuid4().hex[:12],
        KafkaVersion='3.6.0', NumberOfBrokerNodes=1,
        BrokerNodeGroupInfo={'InstanceType': 'kafka.m5.large', 'ClientSubnets': ['subnet-local']})
    arn = result['ClusterArn']
    end = time.monotonic() + 120
    while client.describe_cluster(ClusterArn=arn)['ClusterInfo']['State'] != 'ACTIVE':
        if time.monotonic() > end: raise TimeoutError('MSK not ACTIVE')
        time.sleep(.5)
    bootstrap = client.get_bootstrap_brokers(ClusterArn=arn)['BootstrapBrokerString'].split(',')[0]
    host, port = bootstrap.rsplit(':', 1)
    while True:
        try:
            with socket.create_connection((host, int(port)), 2): break
        except OSError:
            if time.monotonic() > end: raise
            time.sleep(.25)
    create_topic(bootstrap, 'ledger')
    return {'arn': arn, 'bootstrap': bootstrap, 'topic': 'ledger'}


if __name__ == '__main__': print(json.dumps(provision()))
