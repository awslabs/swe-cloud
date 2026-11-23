#!/usr/bin/env bash
# Install the reference destroyer into /app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/destroyer/worker.py" "$here/destroyer/fleet.py" "$here/destroyer/teardown.py" /app/destroyer/
