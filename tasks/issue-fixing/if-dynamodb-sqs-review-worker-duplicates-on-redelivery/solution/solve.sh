#!/usr/bin/env bash
# Install the reference compose-review worker into /app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/compose_review.py" /app/compose_review.py
cp "$here/compose_worker.py" /app/compose_worker.py
