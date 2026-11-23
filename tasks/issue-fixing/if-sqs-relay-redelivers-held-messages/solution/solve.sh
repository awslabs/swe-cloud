#!/usr/bin/env bash
# solution/solve.sh: install the reference consumer and extender into /app/relay.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/relay/consumer.py" /app/relay/consumer.py
cp "$here/relay/extender.py" /app/relay/extender.py
find /app -name '__pycache__' -type d -prune -exec rm -rf {} +
