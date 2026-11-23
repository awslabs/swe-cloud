#!/usr/bin/env bash
# Install the reference server fix into /app/orchestrator.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here"/orchestrator/*.py "$here"/orchestrator/schema.sql /app/orchestrator/
find /app -name __pycache__ -prune -exec rm -rf {} +
