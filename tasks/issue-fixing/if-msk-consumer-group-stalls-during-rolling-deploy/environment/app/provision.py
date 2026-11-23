"""Create a normal development MSK cluster and topic, without fault injection."""
import json
import time
import uuid
import boto3
from botocore.config import Config
from confluent_kafka.admin import AdminClient, NewTopic


def main():
    name = 'dev-' + uuid.uuid4().hex[:10]
    client = boto3.client('kafka', config=Config(read_timeout=600, retries={'max_attempts': 1}))
    arn = client.create_cluster(ClusterName=name, KafkaVersion='3.6.0', NumberOfBrokerNodes=1,
                                BrokerNodeGroupInfo={'ClientSubnets': ['subnet-local'],
                                                     'InstanceType': 'kafka.m5.large'})['ClusterArn']
    while client.describe_cluster(ClusterArn=arn)['ClusterInfo']['State'] != 'ACTIVE': time.sleep(1)
    bootstrap = client.get_bootstrap_brokers(ClusterArn=arn)['BootstrapBrokerString']
    admin = AdminClient({'bootstrap.servers': bootstrap})
    for attempt in range(20):
        try:
            futures = admin.create_topics([NewTopic(name, 6, 1), NewTopic(name+'-receipts', 1, 1)], request_timeout=5)
            for f in futures.values(): f.result()
            break
        except Exception:
            if attempt == 19: raise
            time.sleep(1)
    print(json.dumps(dict(bootstrap=bootstrap, topic=name, output_topic=name+'-receipts', group=name, cluster_arn=arn)))


if __name__ == '__main__': main()
