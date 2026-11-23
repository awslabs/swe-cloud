#!/usr/bin/env bash
# Install the reference commit handler into /app.
set -euo pipefail
cp "$(dirname "$0")/handler.py" /app/strata/handler.py
