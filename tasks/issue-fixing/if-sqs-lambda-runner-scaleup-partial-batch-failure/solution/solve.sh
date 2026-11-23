#!/usr/bin/env bash
# Reference: per-record outcomes through every failure path of the scale-up group loop.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
for f in fleet.py ec2.py scale_up.py lambda_handler.py ssm.py launches.py; do
  cp "$here/control_plane/$f" "/app/control_plane/$f"
done
find /app -name __pycache__ -prune -exec rm -rf {} +
