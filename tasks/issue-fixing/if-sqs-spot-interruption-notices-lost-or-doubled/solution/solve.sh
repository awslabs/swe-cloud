#!/usr/bin/env bash
# Install the reference handler into /app; files resolve relative to this script.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
for f in lease.py kube.py sqs_monitor.py spot_itn.py events.py drain.py __main__.py; do
  cp "$here/nth/$f" "/app/nth/$f"
done
find /app -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
