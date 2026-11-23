#!/usr/bin/env bash
# Install the reference service modules into /app; resolve files relative to this script.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/manifest_service/async_service.py" /app/manifest_service/async_service.py
cp "$here/manifest_service/controller.py" /app/manifest_service/controller.py
find /app -name __pycache__ -prune -exec rm -rf {} +
