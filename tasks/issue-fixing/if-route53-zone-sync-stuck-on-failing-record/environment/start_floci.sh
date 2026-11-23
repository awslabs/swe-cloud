#!/usr/bin/env bash
# environment/start_floci.sh: idempotent boot, then wait for health.
set -euo pipefail
if ! pgrep -x floci >/dev/null 2>&1; then
  nohup /opt/floci/floci >/tmp/floci.log 2>&1 </dev/null &
fi
for _ in $(seq 1 120); do
  if curl -fsS http://localhost:4566/_floci/health >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://localhost:4566/_floci/health >/dev/null 2>&1 || { tail -200 /tmp/floci.log >&2 || true; exit 1; }

# The operator relay in front of Route 53; it must be up before the first reconcile.
mkdir -p /run/r53relay
if ! pgrep -f "python3 /app/relay.py" >/dev/null 2>&1; then
  nohup python3 /app/relay.py >/tmp/relay.log 2>&1 </dev/null &
fi
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:4567/_health >/dev/null 2>&1; then exit 0; fi
  sleep 1
done
tail -200 /tmp/relay.log >&2 || true
exit 1
