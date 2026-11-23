#!/usr/bin/env bash
# tests/test.sh: the verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
# Start from an empty account: stop the emulator (it ignores SIGTERM) and drop whatever state the
# solve phase left behind, then boot it again.
pkill -9 -f '[n]uke[.]py run' >/dev/null 2>&1 || true
pkill -9 '^floci$' >/dev/null 2>&1 || true
sleep 1
rm -rf /opt/floci/data/* 2>/dev/null || true
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: emulator did not become healthy" | tee /logs/verifier/environment.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
cd /tests
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider -W ignore::DeprecationWarning /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if grep -q "^E .*environment: the emulator is not answering" /logs/verifier/tests.txt; then
  echo "environment: emulator not live during the verifier" > /logs/verifier/environment.txt
fi
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
