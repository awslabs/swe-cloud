#!/usr/bin/env bash
# The verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: Floci did not become healthy on :4566" | tee /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
if ! python3 -c "import boto3; boto3.client('s3', endpoint_url='http://localhost:4566', region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test').list_buckets()" 2>/logs/verifier/environment.txt; then
  echo "environment: S3 on :4566 is not answering" | tee -a /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
cd /tests
python3 -m pytest -q -p no:cacheprovider -rA /tests/test_orphan_clean.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
