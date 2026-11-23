#!/usr/bin/env bash
# The verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! floci-venue up; then
  echo "environment: floci venue did not come up" > /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
if ! curl -sf "${AWS_ENDPOINT_URL:-http://engines:4566}/_floci/health" >/dev/null; then
  echo "environment: Floci not healthy" > /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
cd /tests
export PYTHONDONTWRITEBYTECODE=1
python3 -m pytest -q -p no:cacheprovider -rA /tests/test_stepq.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
