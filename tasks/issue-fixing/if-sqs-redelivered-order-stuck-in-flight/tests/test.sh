#!/usr/bin/env bash
# tests/test.sh: the verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: Floci did not become healthy" | tee /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
if ! python3 -c "import boto3; boto3.client('sqs', endpoint_url='http://localhost:4566', region_name='us-east-1').list_queues()" >/dev/null 2>&1; then
  echo "environment: Floci SQS is not answering" | tee /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
python3 -m pytest -q -p no:cacheprovider -rA /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
