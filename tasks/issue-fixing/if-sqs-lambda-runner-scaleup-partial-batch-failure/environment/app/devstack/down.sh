#!/usr/bin/env bash
# Stop the local dev stack started by devstack/up.sh (the queues stay).
for name in esm fleet github; do
  pid_file="/tmp/devstack/$name.pid"
  [ -f "$pid_file" ] && kill "$(cat "$pid_file")" 2>/dev/null && rm -f "$pid_file"
done
echo "dev stack stopped"
