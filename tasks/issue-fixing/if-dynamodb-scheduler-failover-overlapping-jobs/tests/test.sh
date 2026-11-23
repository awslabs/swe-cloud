#!/usr/bin/env bash
# tests/test.sh: the verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: floci not healthy before verifier" > /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
if ! curl -fsS http://localhost:4566/_floci/health >/dev/null 2>&1; then
  echo "environment: emulator health endpoint not answering" > /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
python3 -m pytest -q -p no:cacheprovider /tests/test_failover.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
