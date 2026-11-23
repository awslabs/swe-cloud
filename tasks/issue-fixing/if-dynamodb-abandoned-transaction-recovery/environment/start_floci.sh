#!/usr/bin/env bash
# Idempotent boot, then wait for health.
set -euo pipefail
if ! pgrep -x floci >/dev/null 2>&1; then
  nohup /opt/floci/floci >/tmp/floci.log 2>&1 </dev/null &
fi
for _ in $(seq 1 120); do
  if curl -fsS http://localhost:4566/_floci/health >/dev/null 2>&1; then exit 0; fi
  sleep 1
done
tail -200 /tmp/floci.log >&2 || true
exit 1
