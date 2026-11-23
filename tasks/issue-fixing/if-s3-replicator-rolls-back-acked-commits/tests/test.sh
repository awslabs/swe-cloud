#!/usr/bin/env bash
# tests/test.sh: the verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! bash /usr/local/bin/start_floci.sh; then
  echo "ENVIRONMENT: floci did not become healthy on :4566" | tee /logs/verifier/tests.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
cd /tests
python3 -m pytest -q -p no:cacheprovider -W ignore::DeprecationWarning /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
