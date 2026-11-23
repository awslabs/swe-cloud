#!/usr/bin/env bash
# Installs the reference fenced_worker into /app.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here"/fenced_worker/lockclient.py "$here"/fenced_worker/ledger.py "$here"/fenced_worker/worker.py /app/fenced_worker/
