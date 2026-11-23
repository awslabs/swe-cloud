#!/usr/bin/env bash
# The verifier owns its score even when environment startup or collection fails.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
bash /usr/local/bin/start_floci.sh || { echo environment: emulator startup failed > /logs/verifier/tests.txt; exit 1; }
python3 -c 'import urllib.request; urllib.request.urlopen("http://localhost:4566/_floci/health", timeout=5).read()' || { echo environment: emulator preflight failed > /logs/verifier/tests.txt; exit 1; }
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
