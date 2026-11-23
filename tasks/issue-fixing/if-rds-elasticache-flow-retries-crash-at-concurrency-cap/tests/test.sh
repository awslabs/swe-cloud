#!/usr/bin/env bash
# The verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! floci-venue up; then
  echo "floci-venue up failed" > /logs/verifier/environment
  exit 1
fi
# Leftovers from the agent's own experiments must not answer for the processes under test.
pkill -9 -f 'orchestrator\.(server|repossessor|admin)' 2>/dev/null
pkill -9 -f 'fleet\.(worker|supervisor|engine)' 2>/dev/null
for pid in $(ss -ltnpH 'sport = :4200' 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2); do kill -9 "$pid"; done
sleep 1
cd /tests
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider -rA /tests/test_concurrency.py \
  > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
