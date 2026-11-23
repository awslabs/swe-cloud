#!/usr/bin/env bash
# Reference: reachability-based cleanup of tails and uploads on COMPLETE and END, nothing freed on
# restore or when the attempt is cancelled.
set -euo pipefail
cp "$(dirname "$0")/bucket.py" /app/partsink/bucket.py
cp "$(dirname "$0")/sink.py" /app/sink.py
