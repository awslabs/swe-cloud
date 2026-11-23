#!/usr/bin/env bash
# Install the reference replicator into /app.
set -euo pipefail
cp "$(dirname "$0")/replicator.py" /app/strata/replicator.py
