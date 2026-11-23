#!/usr/bin/env bash
# Verifier: brings the venue up, runs the scenario once and the outcome checks, always writes the reward.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! floci-venue up; then
  echo "floci-venue up failed" > /logs/verifier/environment
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
cd /tests
python3 -m pytest -q -p no:cacheprovider -rA /tests/test_relay.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
