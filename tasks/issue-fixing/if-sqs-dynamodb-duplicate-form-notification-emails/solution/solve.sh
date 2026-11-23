#!/usr/bin/env bash
# Install the reference worker, callback and data layer into /app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/reliability/store.py" "$here/reliability/worker.py" "$here/reliability/callback.py" /app/reliability/
find /app -name __pycache__ -type d -prune -exec rm -rf {} +
