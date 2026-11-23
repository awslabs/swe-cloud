#!/usr/bin/env bash
# Install the reference implementation into /app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/reference.py" /app/dispatcher.py
cp "$here/decision.json" /app/decision.json
