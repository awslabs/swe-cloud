#!/usr/bin/env bash
# Install the reference compactor into /app.
set -euo pipefail
cp "$(dirname "$0")/reference.py" /app/worker.py
