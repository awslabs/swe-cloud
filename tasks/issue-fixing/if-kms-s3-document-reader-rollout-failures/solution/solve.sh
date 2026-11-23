#!/usr/bin/env bash
set -euo pipefail
here="$(dirname "$0")"
cp "$here/keyservice.py" /app/keyservice.py
cp "$here/migration.py" /app/migration.py
cp "$here/migrator.py" /app/migrator.py
