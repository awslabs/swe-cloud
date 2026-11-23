#!/usr/bin/env bash
# Boots the emulator, runs the verifier, and always writes the reward file.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
echo 0 > /logs/verifier/reward.txt
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: floci did not become healthy" > /logs/verifier/environment_error
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
# handlers run as the unprivileged handler user; make sure it can read the tree
chmod a+rx /tests 2>/dev/null || true
chmod -R a+rX /app /tests/legacy 2>/dev/null || true
cd /tests
export PYTHONDONTWRITEBYTECODE=1
python3 -m pytest -q -p no:cacheprovider -rA /tests/test_exports.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
