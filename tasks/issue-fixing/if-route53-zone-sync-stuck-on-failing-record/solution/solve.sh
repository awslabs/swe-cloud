#!/usr/bin/env bash
# Install the reference reconciler into /app.
set -euo pipefail
cp "$(dirname "$0")/reconciler.py" /app/reconciler.py
chmod +x /app/reconciler.py
