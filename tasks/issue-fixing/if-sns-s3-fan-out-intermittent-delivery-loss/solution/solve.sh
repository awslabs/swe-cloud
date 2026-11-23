#!/usr/bin/env bash
set -euo pipefail
cp "$(dirname "$0")/pump.py" /app/pump.py

cp "$(dirname "$0")/publisher.py" /app/publisher.py
