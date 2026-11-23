#!/usr/bin/env bash
# Install the reference implementation into /app; resolve files relative to this script.
set -euo pipefail
cp "$(dirname "$0")/worker.py" /app/worker.py
