#!/usr/bin/env bash
# Boots the emulator, runs the fault-injection trials, and always writes the reward file.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
if ! bash /usr/local/bin/start_floci.sh; then
  echo "environment: Floci did not become healthy" > /logs/verifier/tests.txt
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi
# Leftovers from the agent's session must not hold the trial's ports or the lock.
pkill -9 -f fenced_worker 2>/dev/null || true
# Proxy ports are base+1 and base+2: pick a base whose two ports are free right now.
FW_PROXY_BASE_PORT="$(python3 - <<'PY'
import random, socket
def free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()
for _ in range(200):
    b = random.randrange(20000, 32000)  # below the ephemeral range
    if free(b + 1) and free(b + 2):
        print(b)
        break
else:
    print(4600)
PY
)"
export FW_PROXY_BASE_PORT="${FW_PROXY_BASE_PORT:-4600}"
python3 -m pytest -q -rA -p no:cacheprovider /tests/test_failover.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"
