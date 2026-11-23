#!/usr/bin/env bash
# environment/start_floci.sh: idempotent boot, then wait for health.
set -euo pipefail
started=0
if ! pgrep -x floci >/dev/null 2>&1; then
  nohup /opt/floci/floci >/tmp/floci.log 2>&1 </dev/null &
  started=1
fi
for _ in $(seq 1 120); do
  if curl -fsS http://localhost:4566/_floci/health >/dev/null 2>&1; then
    # The account-factory's two tables are part of the deployment, so create
    # them on the boot that started the endpoint, retrying a little in case
    # the endpoint is not taking creates the instant it reports healthy.
    # Strictly best effort: a table that is already there, or an endpoint that
    # will not take the create, is not a failed boot.
    if [ "$started" -eq 1 ]; then
      timeout 30 python3 - <<'PY' >/tmp/aft-tables.log 2>&1 || true
import sys
import time

import boto3
from botocore.config import Config

client = boto3.client(
    "dynamodb",
    region_name="us-east-1",
    endpoint_url="http://localhost:4566",
    aws_access_key_id="test",
    aws_secret_access_key="test",
    config=Config(
        retries={"max_attempts": 2, "mode": "standard"},
        connect_timeout=2,
        read_timeout=5,
    ),
)
TABLES = (("aft-request", "id"), ("aft-sync-state", "pk"))


def present(name):
    try:
        client.describe_table(TableName=name)
        return True
    except Exception:
        return False


for _ in range(5):
    for name, key in TABLES:
        if present(name):
            continue
        definitions = [{"AttributeName": key, "AttributeType": "S"}]
        schema = [{"AttributeName": key, "KeyType": "HASH"}]
        for kwargs in (
            {"BillingMode": "PAY_PER_REQUEST"},
            {"ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5}},
        ):
            try:
                client.create_table(
                    TableName=name, AttributeDefinitions=definitions,
                    KeySchema=schema, **kwargs,
                )
                break
            except Exception:
                continue
    if all(present(name) for name, _ in TABLES):
        sys.exit(0)
    time.sleep(1)
PY
    fi
    exit 0
  fi
  sleep 1
done
tail -200 /tmp/floci.log >&2 || true
exit 1
