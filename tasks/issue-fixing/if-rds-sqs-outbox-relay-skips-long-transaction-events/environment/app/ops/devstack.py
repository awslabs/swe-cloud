"""Dev stack: one RDS PostgreSQL instance with the ledger schema, and the ledger-events FIFO queue.

    eval "$(python3 /app/ops/devstack.py up)"     # create (or reuse) and print the relay's env
    python3 /app/ops/devstack.py reset            # empty the outbox and queue, checkpoint back to 0
    python3 /app/ops/devstack.py env [--writer|--admin]   # print env for a role again

The relay connects as outbox_relay; producers (tools/emit.py) as app_writer; --admin is the
instance's master user, for poking around with psql-like sessions in Python.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.cfn_deploy import deploy

import boto3
import botocore.config
import psycopg

INSTANCE = "ledger-dev"
QUEUE = "ledger-events-dev.fifo"
MASTER_USER, MASTER_PASSWORD = "ledger_admin", "ledger-admin-dev"
PASSWORDS = {"outbox_relay": "relay-dev", "app_writer": "writer-dev"}
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rds():
    return boto3.client("rds", config=botocore.config.Config(read_timeout=600, retries={"max_attempts": 1}))


def _endpoint():
    rds = _rds()
    try:
        inst = rds.describe_db_instances(DBInstanceIdentifier=INSTANCE)["DBInstances"][0]
    except rds.exceptions.DBInstanceNotFoundFault:
        rds.create_db_instance(DBInstanceIdentifier=INSTANCE, Engine="postgres", EngineVersion="16.3",
                               DBInstanceClass="db.t3.micro", AllocatedStorage=20, DBName="ledger",
                               MasterUsername=MASTER_USER, MasterUserPassword=MASTER_PASSWORD)
        inst = None
    while inst is None or inst["DBInstanceStatus"] != "available":
        time.sleep(1)
        inst = rds.describe_db_instances(DBInstanceIdentifier=INSTANCE)["DBInstances"][0]
    return inst["Endpoint"]["Address"], inst["Endpoint"]["Port"]


def _admin(host, port):
    deadline = time.monotonic() + 30
    while True:
        try:
            return psycopg.connect(host=host, port=port, dbname="ledger", user=MASTER_USER,
                                   password=MASTER_PASSWORD, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def _queue_url():
    sqs = boto3.client("sqs")
    return deploy(boto3.client("cloudformation"), "queue-" + INSTANCE,
                  Path(HERE) / "infra/seed.template.json", QueueName=QUEUE)["QueueUrl"]


def _env(host, port, role):
    if role == "admin":
        user, password = MASTER_USER, MASTER_PASSWORD
    else:
        user, password = role, PASSWORDS[role]
    lines = [f"export PGHOST={host}", f"export PGPORT={port}", "export PGDATABASE=ledger",
             f"export PGUSER={user}", f"export PGPASSWORD={password}",
             f"export OUTBOX_QUEUE_URL={_queue_url()}",
             f"export AWS_ENDPOINT_URL={os.environ.get('AWS_ENDPOINT_URL', 'http://engines:4566')}",
             f"export AWS_REGION={os.environ.get('AWS_REGION', 'us-east-1')}"]
    return "\n".join(lines)


def up():
    host, port = _endpoint()
    with _admin(host, port) as conn:
        if conn.execute("select to_regclass('public.outbox')").fetchone()[0] is None:
            conn.execute(open(os.path.join(HERE, "db", "schema.sql")).read())
            conn.execute(open(os.path.join(HERE, "db", "roles.sql")).read())
            for role, password in PASSWORDS.items():
                conn.execute(f"alter role {role} password '{password}'")
    print(_env(host, port, "outbox_relay"))


def reset():
    host, port = _endpoint()
    with _admin(host, port) as conn:
        conn.execute("truncate outbox")
        conn.execute("alter sequence outbox_position_seq restart with 1")
        conn.execute("update relay_checkpoint set position = 0, meta = '{}', updated_at = now()")
    boto3.client("sqs").purge_queue(QueueUrl=_queue_url())
    print("reset", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["up", "reset", "env"])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--writer", action="store_const", dest="role", const="app_writer")
    g.add_argument("--admin", action="store_const", dest="role", const="admin")
    args = p.parse_args()
    if args.command == "up":
        up()
    elif args.command == "reset":
        reset()
    else:
        host, port = _endpoint()
        print(_env(host, port, args.role or "outbox_relay"))


if __name__ == "__main__":
    main()
