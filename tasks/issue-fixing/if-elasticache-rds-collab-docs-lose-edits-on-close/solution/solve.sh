#!/usr/bin/env bash
# Install the reference: the store lock is waited for (never skipped), kept extended for the whole
# store, and bypassed only when Valkey is unreachable; the Database store becomes a conditional
# merge-and-retry on documents.version.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/redis_ext.py" /app/docserver/redis_ext.py
cp "$here/database.py" /app/docserver/database.py
