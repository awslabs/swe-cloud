#!/usr/bin/env bash
# Install the reference worker into /app.
set -euo pipefail
cp "$(dirname "$0")/reference.py" /app/sequencer.py
chmod +x /app/sequencer.py
