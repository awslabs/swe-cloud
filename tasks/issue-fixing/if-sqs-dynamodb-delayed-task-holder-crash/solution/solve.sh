#!/usr/bin/env bash
# solution/solve.sh: install the reference into /app; resolve files relative to this script.
set -euo pipefail
cp "$(dirname "$0")/reference/worker.py" /app/worker.py
cp "$(dirname "$0")/reference/store.py" /app/store.py
