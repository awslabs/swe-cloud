#!/usr/bin/env bash
# Install the reference implementation into /app: the lock provider and storage client that
# reconcile ambiguous conditional-write answers with a per-acquisition id in the lock body, and a
# ledger commit whose counter write is conditional on the counter version it read.
set -euo pipefail
here="$(dirname "$0")"
cp "$here/reference/tablelock/lockfile.py" /app/tablelock/lockfile.py
cp "$here/reference/tablelock/storage.py"  /app/tablelock/storage.py
cp "$here/reference/tablelock/provider.py" /app/tablelock/provider.py
cp "$here/reference/tablelock/ledger.py"   /app/tablelock/ledger.py
