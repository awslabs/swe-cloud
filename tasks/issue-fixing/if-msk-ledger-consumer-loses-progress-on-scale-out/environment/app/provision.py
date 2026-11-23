"""Provision a development ledger and print its connection arguments."""
import json
import os
import time
import uuid
import boto3
from botocore.config import Config
from confluent_kafka.admin import AdminClient, NewTopic


def main():
    k = boto3.client('kafka', endpoint_url=os.environ['AWS_ENDPOINT_URL'],
                     config=Config(read_timeout=600, retries={'max_attempts': 1}))
    name = 'ledger-' + uuid.uuid4().hex[:10]
    arn = k.create_cluster(ClusterName=name, KafkaVersion='3.6.0',
        NumberOfBrokerNodes=1, BrokerNodeGroupInfo={'InstanceType': 'kafka.m5.large',
        'ClientSubnets': ['subnet-local'], 'StorageInfo': {'EbsStorageInfo': {'VolumeSize': 10}}})['ClusterArn']
    while k.describe_cluster(ClusterArn=arn)['ClusterInfo']['State'] != 'ACTIVE':
        time.sleep(1)
    broker = k.get_bootstrap_brokers(ClusterArn=arn)['BootstrapBrokerString']
    for i in range(20):
        try:
            admin = AdminClient({'bootstrap.servers': broker})
            f = admin.create_topics([NewTopic('ledger', 6, 1)])
            f['ledger'].result(10)
            break
        except Exception:
            if i == 19:
                raise
            time.sleep(1)
    print(json.dumps({'bootstrap': broker, 'topic': 'ledger', 'group': name}))


if __name__ == '__main__':
    main()
