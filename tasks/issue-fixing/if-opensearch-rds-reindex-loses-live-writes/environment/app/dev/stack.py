"""A development stack on the local AWS endpoint: the catalog database, the search domain, the export API.

    python3 -m dev.stack up [--rows 2000]     provision (idempotent), seed, build products_v1 behind the alias
    . /tmp/catalog-dev/env                     export OPENSEARCH_URL, INDEXER_DSN, CATALOG_DSN, CATALOG_EXPORT_URL, HOOK_URL
    python3 -m dev.stack reset [--rows 2000]  re-seed the table and rebuild products_v1 behind the alias
    python3 -m catalog.export_api &            the export API on :8701 (needs CATALOG_DSN)
    bin/sync-server &                          the hook on :8702
    python3 -m dev.stack change update --id 17 --name "Desk lamp"   one app change: commit, then the hook
    python3 -m dev.stack change delete --id 42
    python3 -m dev.stack change insert --name "New thing" --price-cents 999 --brand acme
    bin/reindex --alias products --schema-version 2

Run from /app. The first ``up`` takes a minute or two (it pulls and starts the engines).
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog import writes  # noqa: E402
from search_sync import documents  # noqa: E402
from search_sync.opensearch import OpenSearch  # noqa: E402

NAME = "catalog-dev"
MASTER_PASSWORD = "catalog-dev-master"
INDEXER_PASSWORD = "catalog-dev-indexer"
ENV_FILE = Path("/tmp/catalog-dev/env")
BRANDS = ["acme", "globex", "initech", "umbrella", "hooli", "stark", "wayne", "wonka"]
NOUNS = ["lamp", "desk", "chair", "kettle", "mug", "backpack", "headphones", "notebook", "blender", "rug"]
AWS = Config(read_timeout=600, retries={"max_attempts": 1})


def provision() -> tuple[dict, str]:
    rds = boto3.client("rds", config=AWS)
    search = boto3.client("opensearch", config=AWS)
    try:
        rds.create_db_instance(DBInstanceIdentifier=NAME, Engine="postgres", EngineVersion="16.3",
                               DBInstanceClass="db.t3.micro", AllocatedStorage=20,
                               MasterUsername="catalog", MasterUserPassword=MASTER_PASSWORD)
    except rds.exceptions.DBInstanceAlreadyExistsFault:
        pass
    try:
        search.create_domain(DomainName=NAME, EngineVersion="OpenSearch_2.11")
    except search.exceptions.ResourceAlreadyExistsException:
        pass
    while True:
        db = rds.describe_db_instances(DBInstanceIdentifier=NAME)["DBInstances"][0]
        domain = search.describe_domain(DomainName=NAME)["DomainStatus"]
        if db["DBInstanceStatus"] == "available" and db.get("Endpoint") and domain.get("Endpoint"):
            endpoint = domain["Endpoint"]
            return db["Endpoint"], endpoint if endpoint.startswith("http") else f"http://{endpoint}"
        time.sleep(2)


def connect_retry(dsn: str) -> psycopg.Connection:
    for attempt in range(60):
        try:
            return psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError:
            if attempt == 59:
                raise
            time.sleep(1)


def wait_search(url: str) -> OpenSearch:
    client = OpenSearch(url)
    for attempt in range(120):
        try:
            client.request("GET", "/")
            return client
        except OSError:
            if attempt == 119:
                raise
            time.sleep(1)
    return client


def setup_database(master: psycopg.Connection) -> None:
    master.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catalog_indexer') THEN "
        f"CREATE ROLE catalog_indexer LOGIN PASSWORD '{INDEXER_PASSWORD}'; END IF; END $$"
    )
    master.execute("CREATE SCHEMA IF NOT EXISTS reindex_aux AUTHORIZATION catalog_indexer")
    master.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")


def seed(master: psycopg.Connection, search: OpenSearch, rows: int) -> None:
    rng = random.Random(7)
    master.execute("DROP TABLE IF EXISTS products")
    master.execute((Path(__file__).resolve().parents[1] / "catalog" / "schema.sql").read_text().split("-- The search")[0])
    with master.cursor() as cur:
        cur.executemany(
            "INSERT INTO products (name, price_cents, brand, updated_at) VALUES (%s, %s, %s, now())",
            [(f"{rng.choice(NOUNS).title()} {i}", rng.randrange(199, 99999), rng.choice(BRANDS)) for i in range(1, rows + 1)],
        )
    status, indices = search.request("GET", "/_cat/indices", params={"format": "json"})
    for entry in indices:
        if entry["index"].startswith("products"):
            search.request("DELETE", f"/{entry['index']}")
    search.create_index("products_v1", documents.index_body(1))
    current = master.execute("SELECT id, name, price_cents FROM products ORDER BY id").fetchall()
    for start in range(0, len(current), 1000):
        search.bulk([({"index": {"_index": "products_v1", "_id": str(pid)}}, {"name": name, "price_cents": price})
                     for pid, name, price in current[start:start + 1000]])
    search.request("POST", "/_aliases", {"actions": [{"add": {"index": "products_v1", "alias": "products"}}]})
    search.refresh("products")


def env_lines(db: dict, search_url: str) -> str:
    host, port = db["Address"], db["Port"]
    return "\n".join([
        f"export OPENSEARCH_URL={search_url}",
        f"export INDEXER_DSN='host={host} port={port} dbname=postgres user=catalog_indexer password={INDEXER_PASSWORD}'",
        f"export CATALOG_DSN='host={host} port={port} dbname=postgres user=catalog password={MASTER_PASSWORD}'",
        "export CATALOG_EXPORT_URL=http://127.0.0.1:8701",
        "export HOOK_URL=http://127.0.0.1:8702/on_commit",
    ]) + "\n"


def cmd_up(args) -> int:
    db, search_url = provision()
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(env_lines(db, search_url))
    master = connect_retry(f"host={db['Address']} port={db['Port']} dbname=postgres user=catalog password={MASTER_PASSWORD}")
    search = wait_search(search_url)
    setup_database(master)
    if args.reset or not search.alias_indices("products") or not master.execute("SELECT to_regclass('public.products')").fetchone()[0]:
        seed(master, search, args.rows)
    print(ENV_FILE.read_text(), end="")
    print(f"# written to {ENV_FILE}; source it", file=sys.stderr)
    return 0


def cmd_change(args) -> int:
    conn = psycopg.connect(os.environ["CATALOG_DSN"], autocommit=True)
    hook = os.environ.get("HOOK_URL", "http://127.0.0.1:8702/on_commit")
    if args.op == "insert":
        print(writes.insert_product(conn, hook, args.name or "New product", args.price_cents or 1000, args.brand or "acme"))
    elif args.op == "update":
        fields = {k: v for k, v in (("name", args.name), ("price_cents", args.price_cents), ("brand", args.brand)) if v is not None}
        print(writes.update_product(conn, hook, args.id, **(fields or {"price_cents": random.randrange(199, 99999)})))
    else:
        print(writes.delete_product(conn, hook, args.id))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--rows", type=int, default=2000)
    up.set_defaults(func=cmd_up, reset=False)
    reset = sub.add_parser("reset")
    reset.add_argument("--rows", type=int, default=2000)
    reset.set_defaults(func=cmd_up, reset=True)
    change = sub.add_parser("change")
    change.add_argument("op", choices=["insert", "update", "delete"])
    change.add_argument("--id", type=int)
    change.add_argument("--name")
    change.add_argument("--price-cents", type=int)
    change.add_argument("--brand")
    change.set_defaults(func=cmd_change)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
