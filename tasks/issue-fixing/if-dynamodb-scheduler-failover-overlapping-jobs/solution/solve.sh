#!/usr/bin/env bash
# solution/solve.sh: install the reference into /app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/reference/executions.py" /app/executions.py
cp "$here/reference/scheduler.py" /app/scheduler.py
cp "$here/reference/worker.py" /app/worker.py
cp "$here/reference/control.py" /app/control.py
