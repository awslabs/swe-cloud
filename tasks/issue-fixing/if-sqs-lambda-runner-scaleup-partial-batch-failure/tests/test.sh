#!/usr/bin/env bash
# Verifier: bring the venue up, run the scenarios, always write /logs/verifier/reward.txt.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! floci-venue up; then
  echo "environment: Floci did not come up" | tee /logs/verifier/environment-marker.txt
  exit 1
fi
if ! python3 - <<'PY'
import boto3, sys
from botocore.config import Config
c = Config(retries={"max_attempts": 3}, connect_timeout=5, read_timeout=10)
try:
    boto3.client("sqs", config=c).list_queues()
    boto3.client("ssm", config=c).describe_parameters(MaxResults=1)
except Exception as error:
    print(f"environment: Floci SQS/SSM not answering: {error!r}")
    sys.exit(1)
PY
then
  echo "environment: Floci SQS/SSM preflight failed" > /logs/verifier/environment-marker.txt
  exit 1
fi
cd /tests
VERIFIER_LOG_DIR=/logs/verifier python3 -m pytest -q -p no:cacheprovider -rA /tests/test_scale_up.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
