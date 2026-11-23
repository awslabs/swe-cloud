"""Provision a development stack on the AWS endpoint (Floci): an RDS PostgreSQL instance for the
orchestrator's tables and an ElastiCache cluster for lease storage.

    python -m orchestrator.devstack up [--name dev]     create (or reuse) and print the exports
    python -m orchestrator.devstack env [--name dev]    print the exports of an existing stack
    python -m orchestrator.devstack down [--name dev]   delete both

`eval "$(python -m orchestrator.devstack up)"` sets PG_DSN and VALKEY_URL in your shell.
"""

from __future__ import annotations

import argparse
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

USER = "orchestrator"
PASSWORD = "orchestrator-dev-pw"
DBNAME = "orchestrator"
_CFG = Config(read_timeout=600, retries={"max_attempts": 1})


def _rds():
    return boto3.client("rds", config=_CFG)


def _cache():
    return boto3.client("elasticache", config=_CFG)


def _wait(describe, ready, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = describe()
        if ready(item):
            return item
        time.sleep(2)
    raise SystemExit("timed out waiting for the resource to become available")


def up(name: str) -> tuple[str, str]:
    rds, cache = _rds(), _cache()
    try:
        rds.create_db_instance(
            DBInstanceIdentifier=name, Engine="postgres", EngineVersion="16.3",
            DBInstanceClass="db.t3.micro", AllocatedStorage=20,
            MasterUsername=USER, MasterUserPassword=PASSWORD, DBName=DBNAME,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "DBInstanceAlreadyExists":
            raise
    try:
        cache.create_cache_cluster(CacheClusterId=name, Engine="redis",
                                   CacheNodeType="cache.t3.micro", NumCacheNodes=1)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "CacheClusterAlreadyExists":
            raise
    return env(name)


def env(name: str) -> tuple[str, str]:
    rds, cache = _rds(), _cache()
    db = _wait(lambda: rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0],
               lambda d: d["DBInstanceStatus"] == "available")
    cc = _wait(lambda: cache.describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)["CacheClusters"][0],
               lambda c: c["CacheClusterStatus"] == "available")
    pg = f"postgresql://{USER}:{PASSWORD}@{db['Endpoint']['Address']}:{db['Endpoint']['Port']}/{DBNAME}"
    node = cc["CacheNodes"][0]["Endpoint"]
    return pg, f"redis://{node['Address']}:{node['Port']}/0"


def down(name: str) -> None:
    try:
        _rds().delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True)
    except ClientError as exc:
        print(f"rds: {exc}", file=sys.stderr)
    try:
        _cache().delete_cache_cluster(CacheClusterId=name)
    except ClientError as exc:
        print(f"elasticache: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "env", "down"])
    parser.add_argument("--name", default="orchestrator-dev")
    args = parser.parse_args()
    if args.command == "down":
        down(args.name)
        return
    pg, valkey = up(args.name) if args.command == "up" else env(args.name)
    print(f"export PG_DSN='{pg}'")
    print(f"export VALKEY_URL='{valkey}'")


if __name__ == "__main__":
    main()
