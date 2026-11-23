#!/usr/bin/env python3
"""Create a development MSK cluster and ledger topic on the local AWS endpoint, and print the bootstrap.

    python3 scripts/dev_cluster.py [--cluster NAME] [--topic TOPIC]

Reuses the cluster if one with that name already exists. The first cluster on a fresh environment also
pulls the broker image, so allow a minute or so.
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3
from botocore.config import Config
from confluent_kafka.admin import AdminClient, NewTopic


def ensure_cluster(name: str) -> str:
    kafka = boto3.client("kafka", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    arn = next((c["ClusterArn"] for c in kafka.list_clusters_v2().get("ClusterInfoList", [])
                if c["ClusterName"] == name), None)
    if arn is None:
        arn = kafka.create_cluster(
            ClusterName=name, KafkaVersion="3.6.0", NumberOfBrokerNodes=1,
            BrokerNodeGroupInfo={"InstanceType": "kafka.m5.large", "ClientSubnets": ["subnet-dev"]},
        )["ClusterArn"]
    while kafka.describe_cluster(ClusterArn=arn)["ClusterInfo"]["State"] != "ACTIVE":
        time.sleep(1)
    return kafka.get_bootstrap_brokers(ClusterArn=arn)["BootstrapBrokerString"]


def ensure_topic(bootstrap: str, topic: str) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    deadline = time.monotonic() + 30
    while True:
        try:
            if topic in admin.list_topics(timeout=5).topics:
                return
            admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])[topic].result()
            return
        except Exception:  # broker still starting
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cluster", default="ledger-dev")
    parser.add_argument("--topic", default="ledger")
    args = parser.parse_args()
    bootstrap = ensure_cluster(args.cluster)
    ensure_topic(bootstrap, args.topic)
    print(bootstrap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
