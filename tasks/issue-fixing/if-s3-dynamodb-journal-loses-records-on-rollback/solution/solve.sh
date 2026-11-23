#!/usr/bin/env bash
# solution/solve.sh: install the reference into /app; resolve files relative to this script.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/records.py" /app/records.py
cp "$here/service.py" /app/service.py
cp "$here/sweeper.py" /app/sweeper.py
cp "$here/deployer.py" /app/deployer.py
