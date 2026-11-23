#!/usr/bin/env bash
# Install the reference implementation into /app.
set -euo pipefail
cp "$(dirname "$0")/reference.py" /app/aft_sync.py
chmod 644 /app/aft_sync.py
