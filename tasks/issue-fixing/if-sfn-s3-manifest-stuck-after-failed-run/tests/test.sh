#!/usr/bin/env bash
# tests/test.sh: the verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: the emulator did not become healthy" > /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
cd /tests
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -rA -p no:cacheprovider /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 3 ]; then
  echo "environment: the emulator stopped answering during the run" > /logs/verifier/environment.txt
fi
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
