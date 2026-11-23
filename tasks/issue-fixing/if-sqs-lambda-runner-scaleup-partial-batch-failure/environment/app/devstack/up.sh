#!/usr/bin/env bash
# Start the local dev stack: build queue + DLQ on Floci, fleet API, code-host API, event source mapping.
# Logs go to /tmp/devstack/*.log; stop it with devstack/down.sh.
set -euo pipefail
app="$(cd "$(dirname "$0")/.." && pwd)"
logs=/tmp/devstack && mkdir -p "$logs"
queue="${BUILD_QUEUE:-ghr-dev-queued-builds}"

python3 - "$queue" "$app/deploy/scale-up.json" <<'PY'
import hashlib, json, sys, boto3
from pathlib import Path
app_root = Path(sys.argv[2]).resolve().parents[1]
sys.path.insert(0, str(app_root))
from infra.cfn_deploy import deploy as deploy_stack
queue, deploy = sys.argv[1], json.load(open(sys.argv[2]))
q = deploy["build_queue"]
deploy_stack(boto3.client("cloudformation"),
             "runner-dev-queues-" + hashlib.sha256(queue.encode()).hexdigest()[:24],
             app_root / "infra/seed.template.json",
             DlqName=f"{queue}-dlq", QueueName=queue,
             VisibilityTimeout=q["visibility_timeout_seconds"],
             RetentionPeriod=q["message_retention_seconds"],
             MaxReceiveCount=q["redrive_policy"]["max_receive_count"])
print(f"queues ready: {queue}, {queue}-dlq")
PY

export FLEET_API_URL="${FLEET_API_URL:-http://127.0.0.1:8081}" GITHUB_API_URL="${GITHUB_API_URL:-http://127.0.0.1:8082}"
cd "$app"
nohup python3 devstack/fleet_api.py --port "${FLEET_API_URL##*:}" >"$logs/fleet.log" 2>&1 & echo $! >"$logs/fleet.pid"
nohup python3 devstack/github_api.py --port "${GITHUB_API_URL##*:}" >"$logs/github.log" 2>&1 & echo $! >"$logs/github.pid"
sleep 0.5
nohup python3 devstack/esm.py --queue "$queue" >"$logs/esm.log" 2>&1 & echo $! >"$logs/esm.pid"
echo "dev stack up: fleet $FLEET_API_URL, github $GITHUB_API_URL, esm on $queue (logs in $logs)"
