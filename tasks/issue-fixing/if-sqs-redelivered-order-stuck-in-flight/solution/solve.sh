#!/usr/bin/env bash
# Reference: a per-MessageId in-flight record that keeps the newest delivery's lease renewed and
# applies the outcome through it, with a heartbeat scheduled from each send so slow answers cannot
# let the lease lapse.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app="${APP_DIR:-/app}"
cp "$here/relay/agent.py" "$app/relay/agent.py"
cp "$here/relay/lease.py" "$app/relay/lease.py"
