#!/usr/bin/env bash
set -euo pipefail
cp "$(dirname "$0")/persistence.py" /app/persistence.py
cp "$(dirname "$0")/idempotency.py" /app/idempotency.py
cp "$(dirname "$0")/reservations.py" /app/reservations.py
