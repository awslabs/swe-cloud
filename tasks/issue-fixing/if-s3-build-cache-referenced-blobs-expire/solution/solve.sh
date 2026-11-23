#!/usr/bin/env bash
# Reference: install the patched disk tier and S3 proxy into /app.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${CACHESVC_APP:-/app}"
cp "$HERE/cachesvc/s3proxy.py" "$TARGET/cachesvc/s3proxy.py"
cp "$HERE/cachesvc/disk.py" "$TARGET/cachesvc/disk.py"
