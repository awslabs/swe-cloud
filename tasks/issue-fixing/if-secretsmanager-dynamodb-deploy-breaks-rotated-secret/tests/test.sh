#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
# A default score survives verifier startup failures as well as test failures.
echo 0 > /logs/verifier/reward.txt
bash /usr/local/bin/start_floci.sh || { echo 'environment: emulator unavailable' > /logs/verifier/tests.txt; cat /logs/verifier/tests.txt; exit 1; }
python3 -m pytest -q -p no:cacheprovider /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
