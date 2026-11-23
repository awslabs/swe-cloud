#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/authority.py" "$here/execute_step.py" "$here/run_worker.py" /app/solution/
