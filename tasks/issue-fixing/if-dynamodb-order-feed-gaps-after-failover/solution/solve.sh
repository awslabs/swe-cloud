#!/usr/bin/env bash
set -euo pipefail
cp "$(dirname "$0")/reference.py" /app/cdc_reader.py
cp "$(dirname "$0")/checkpoints.py" /app/checkpoints.py
