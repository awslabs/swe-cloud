#!/usr/bin/env bash
# Reference: record the execution before starting it, start a recorded-but-invisible execution under
# its own name, treat a stopped run as a failure, and where the previous version may have left an
# unrecorded start, look for it for the visibility grace before starting one.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/job.py" /app/durable_task/job.py
cp "$here/resumable.py" /app/durable_task/resumable.py
cp "$here/store.py" /app/durable_task/store.py
