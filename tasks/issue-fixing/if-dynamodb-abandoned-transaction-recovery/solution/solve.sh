#!/usr/bin/env bash
# Install the reference implementation into /app.
set -euo pipefail
cd "$(dirname "$0")"
cp db.py seam.py sweeper.py txnmgr.py protocol.py /app/
