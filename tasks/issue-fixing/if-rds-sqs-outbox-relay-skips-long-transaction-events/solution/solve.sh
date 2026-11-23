#!/usr/bin/env bash
# Reference: the frontier only passes a missing position once a snapshot proves that every
# transaction which could have drawn it has finished (xid bound taken after the observation).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/app/relay.py" /app/relay.py
cp "$here/app/outbox_relay/frontier.py" /app/outbox_relay/frontier.py
cp "$here/app/outbox_relay/store.py" /app/outbox_relay/store.py
