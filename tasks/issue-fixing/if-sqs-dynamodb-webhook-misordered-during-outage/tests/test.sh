#!/usr/bin/env bash
# The verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
if ! bash /usr/local/bin/start_floci.sh; then
  echo 0 > /logs/verifier/reward.txt
  echo "floci-unhealthy" > /logs/verifier/environment.txt
  exit 1
fi
python3 -m pytest -q -p no:cacheprovider /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$rc"
