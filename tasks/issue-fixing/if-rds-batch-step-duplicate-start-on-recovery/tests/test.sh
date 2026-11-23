#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
printf '0\n' > /logs/verifier/reward.txt
if ! floci-venue up; then
  echo 'environment: venue unavailable' > /logs/verifier/tests.txt
  exit 1
fi
verifier_dir=$(mktemp -d /tmp/batch-verifier-XXXXXX)
cd "$verifier_dir"
PYTHONPATH= PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
