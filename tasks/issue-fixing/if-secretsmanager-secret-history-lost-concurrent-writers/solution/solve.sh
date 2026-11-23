#!/usr/bin/env bash
# Reference: install the fixed Secrets Manager store into /app.
set -euo pipefail
cp "$(dirname "$0")/store.py" /app/chambersm/store.py
