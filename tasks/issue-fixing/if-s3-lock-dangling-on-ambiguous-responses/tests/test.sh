#!/usr/bin/env bash
# Boot the emulator, run the verifier, and always write the reward file.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: emulator did not come up" >&2
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
export PYTHONPATH=/tests
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
python3 -m pytest -q -p no:cacheprovider /tests/test_lock.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
