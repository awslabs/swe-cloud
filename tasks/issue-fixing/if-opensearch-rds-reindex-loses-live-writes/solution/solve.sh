#!/usr/bin/env bash
# Install the reference into /app; files resolve relative to this script.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/search_sync/store.py" "$here/search_sync/server.py" "$here/search_sync/reindex.py" /app/search_sync/
