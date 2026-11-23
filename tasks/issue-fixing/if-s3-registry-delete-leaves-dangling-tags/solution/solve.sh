#!/usr/bin/env bash
# Install the reference implementation over the starter.
set -euo pipefail
cp -r "$(dirname "$0")/app/quayside/." /app/quayside/
find /app -name __pycache__ -prune -exec rm -rf {} +
