#!/usr/bin/env python3
"""A development stack for docserver on the local AWS endpoint (Floci).

    python3 dev/devstack.py up          create (or reuse) the dev Postgres and Valkey, apply schema.sql,
                                        and print the environment for docserver
    python3 dev/devstack.py show NAME   print a document's row and its committed history (decoded)

`up` is idempotent. The dev resources are `docserver-dev` in RDS and ElastiCache; the master
credentials below are for this stack only.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import boto3
import psycopg
import pycrdt
from botocore.config import Config

NAME = "docserver-dev"
DB = "docs"
MASTER = ("master", "devmaster-pw")
DOCSERVER_PASSWORD = "docserver-pw"
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
CONFIG = Config(read_timeout=600, retries={"max_attempts": 1})


def _rds_endpoint() -> tuple[str, int]:
    rds = boto3.client("rds", config=CONFIG)
    try:
        instance = rds.describe_db_instances(DBInstanceIdentifier=NAME)["DBInstances"][0]
    except rds.exceptions.DBInstanceNotFoundFault:
        rds.create_db_instance(
            DBInstanceIdentifier=NAME, Engine="postgres", EngineVersion="16.3", DBInstanceClass="db.t3.micro",
            AllocatedStorage=20, DBName=DB, MasterUsername=MASTER[0], MasterUserPassword=MASTER[1],
        )
        instance = None
    while instance is None or instance["DBInstanceStatus"] != "available":
        time.sleep(2)
        instance = rds.describe_db_instances(DBInstanceIdentifier=NAME)["DBInstances"][0]
    return instance["Endpoint"]["Address"], int(instance["Endpoint"]["Port"])


def _valkey_endpoint() -> tuple[str, int]:
    elasticache = boto3.client("elasticache", config=CONFIG)
    try:
        cluster = elasticache.describe_cache_clusters(CacheClusterId=NAME, ShowCacheNodeInfo=True)["CacheClusters"][0]
    except elasticache.exceptions.CacheClusterNotFoundFault:
        elasticache.create_cache_cluster(CacheClusterId=NAME, Engine="redis", CacheNodeType="cache.t3.micro", NumCacheNodes=1)
        cluster = None
    while cluster is None or cluster["CacheClusterStatus"] != "available":
        time.sleep(2)
        cluster = elasticache.describe_cache_clusters(CacheClusterId=NAME, ShowCacheNodeInfo=True)["CacheClusters"][0]
    endpoint = cluster["CacheNodes"][0]["Endpoint"]
    return endpoint["Address"], int(endpoint["Port"])


def _master(host: str, port: int) -> psycopg.Connection:
    for attempt in range(30):
        try:
            return psycopg.connect(host=host, port=port, dbname=DB, user=MASTER[0], password=MASTER[1], autocommit=True, connect_timeout=5)
        except psycopg.OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)


def up() -> int:
    pg_host, pg_port = _rds_endpoint()
    valkey_host, valkey_port = _valkey_endpoint()
    with _master(pg_host, pg_port) as conn:
        conn.execute(SCHEMA.read_text())
        conn.execute(f"ALTER ROLE docserver PASSWORD '{DOCSERVER_PASSWORD}'")
        conn.execute(f"GRANT CONNECT ON DATABASE {DB} TO docserver")
    print(f"export PG_DSN='postgresql://docserver:{DOCSERVER_PASSWORD}@{pg_host}:{pg_port}/{DB}'")
    print(f"export VALKEY_URL='redis://{valkey_host}:{valkey_port}/0'")
    print("export KEY_PREFIX='p'")
    print(f"# master: postgresql://{MASTER[0]}:{MASTER[1]}@{pg_host}:{pg_port}/{DB}", file=sys.stderr)
    return 0


def _edits(state: bytes) -> list[str]:
    doc = pycrdt.Doc()
    doc.apply_update(bytes(state))
    return sorted(doc.get("edits", type=pycrdt.Map).keys())


def show(name: str) -> int:
    pg_host, pg_port = _rds_endpoint()
    with _master(pg_host, pg_port) as conn:
        row = conn.execute("SELECT version, state FROM documents WHERE name = %s", (name,)).fetchone()
        print(f"row: {'absent' if row is None else f'version {row[0]} edits {_edits(row[1])}'}")
        for version, writer, at, state in conn.execute(
            "SELECT version, writer, at, state FROM documents_history WHERE name = %s ORDER BY id", (name,)
        ):
            print(f"  v{version} {writer:<10} {at:%H:%M:%S.%f} {_edits(state)}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "up":
        return up()
    if len(argv) >= 3 and argv[1] == "show":
        return show(argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
