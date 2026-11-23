#!/usr/bin/env bash
# solution/solve.sh: install the reference implementation into /app.
set -euo pipefail
cp "$(dirname "$0")/logstore.py" /app/logstore.py
