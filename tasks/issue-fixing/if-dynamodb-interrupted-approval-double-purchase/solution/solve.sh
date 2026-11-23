#!/usr/bin/env bash
set -euo pipefail
cp "$(dirname "$0")/manager.py" /app/manager.py
cp "$(dirname "$0")/retry.py" /app/retry.py
