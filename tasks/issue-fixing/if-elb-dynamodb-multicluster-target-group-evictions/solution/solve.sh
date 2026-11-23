#!/usr/bin/env bash
# Reference: ownership items in tgb_ownership license deregistration; written ahead of
# RegisterTargets, deleted only after DeregisterTargets; decisions from a fresh listing; binding
# files older than the durably recorded generation are ignored.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/app/controller.py" /app/controller.py
cp "$here/app/tgb/reconcile.py" /app/tgb/reconcile.py
cp "$here/app/tgb/ledger.py" /app/tgb/ledger.py
