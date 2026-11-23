#!/usr/bin/env bash
# Boots the emulator, runs the verifier, and always writes /logs/verifier/reward.txt.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: floci did not become healthy" > /logs/verifier/environment_failure.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
python3 -m pytest -q -p no:cacheprovider -rA /tests/test_shared_target_group.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
