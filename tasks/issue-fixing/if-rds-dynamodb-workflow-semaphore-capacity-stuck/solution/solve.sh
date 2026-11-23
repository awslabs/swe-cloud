#!/usr/bin/env bash
set -euo pipefail
for file in controller.py session.py db.py workflows.py; do
  cp "$(dirname "$0")/$file" "/app/$file"
done
