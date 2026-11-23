#!/usr/bin/env bash
# Install the reference cleaner into /app.
set -euo pipefail
cp "$(dirname "$0")/orphan_clean.py" /app/orphan_clean.py
