#!/usr/bin/env bash
set -euo pipefail
cp "$(dirname "$0")/query.py" /app/sink/query.py
cp "$(dirname "$0")/bulk.py" /app/sink/bulk.py
cp "$(dirname "$0")/main.py" /app/sink/__main__.py
cp "$(dirname "$0")/transport.py" /app/sink/transport.py
cp "$(dirname "$0")/inbox.py" /app/sink/inbox.py
