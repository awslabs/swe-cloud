"""Bring up a development stack on the local AWS endpoint and print the worker's environment.

    eval "$(python3 tools/dev_stack.py)"

Creates (or reuses) an Amazon MQ RabbitMQ broker, applies the platform's vm.destroy definition from
deploy/platform/vm.destroy.json, creates the fleet table, and prints export lines for AMQP_URL,
WORK_QUEUE and FLEET_TABLE. Run ``sandbox-cloud`` alongside so vm-destroy has a cloud API to talk to.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import boto3
import pika
from botocore.config import Config

DEFINITION = Path(__file__).resolve().parent.parent / "deploy" / "platform" / "vm.destroy.json"
USER = "destroyer"
PASSWORD = "destroyer-dev-password"


def broker(name: str) -> str:
    mq = boto3.client("mq", config=Config(read_timeout=600, retries={"max_attempts": 1}))
    found = [b for b in mq.list_brokers()["BrokerSummaries"] if b["BrokerName"] == name]
    if found:
        broker_id = found[0]["BrokerId"]
    else:
        broker_id = mq.create_broker(
            BrokerName=name, EngineType="RABBITMQ", EngineVersion="3.13", HostInstanceType="mq.m5.large",
            DeploymentMode="SINGLE_INSTANCE", PubliclyAccessible=False, AutoMinorVersionUpgrade=True,
            Users=[{"Username": USER, "Password": PASSWORD}],
        )["BrokerId"]
    deadline = time.time() + 300
    while True:
        described = mq.describe_broker(BrokerId=broker_id)
        if described["BrokerState"] == "RUNNING":
            break
        if time.time() > deadline:
            sys.exit(f"broker {name} is still {described['BrokerState']}")
        time.sleep(2)
    endpoint = described["BrokerInstances"][0]["Endpoints"][0]
    return endpoint.replace("amqp://", f"amqp://{USER}:{quote(PASSWORD)}@") + "/%2F"


def apply_definition(amqp_url: str) -> str:
    definition = json.loads(DEFINITION.read_text())
    for attempt in range(30):
        try:
            connection = pika.BlockingConnection(pika.URLParameters(amqp_url))
            break
        except pika.exceptions.AMQPConnectionError:
            time.sleep(1)
    else:
        sys.exit(f"cannot connect to {amqp_url}")
    channel = connection.channel()
    channel.queue_declare(definition["queue"], durable=definition["durable"], auto_delete=definition["auto_delete"], arguments=definition["arguments"])
    connection.close()
    return definition["queue"]


def fleet_table(name: str) -> str:
    ddb = boto3.client("dynamodb")
    if name not in ddb.list_tables()["TableNames"]:
        ddb.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "vm_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "vm_id", "AttributeType": "S"}],
        )
        ddb.get_waiter("table_exists").wait(TableName=name)
    return name


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dev_stack")
    parser.add_argument("--broker", default="destroyer-dev")
    parser.add_argument("--fleet-table", default="vm-fleet-dev")
    args = parser.parse_args(argv)
    amqp_url = broker(args.broker)
    queue = apply_definition(amqp_url)
    table = fleet_table(args.fleet_table)
    print(f"export AMQP_URL='{amqp_url}'")
    print(f"export WORK_QUEUE='{queue}'")
    print(f"export FLEET_TABLE='{table}'")


if __name__ == "__main__":
    main()
