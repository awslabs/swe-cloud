#!/usr/bin/env bash
# Pipeline step "set-secret": stores one value for a service.
#
#   ci/set-secret.sh <service> <key> <value>
#
# Every invocation is its own job on an ephemeral runner, assuming the service's
# chambersm-writer role (iam/chambersm-writer.json). Jobs from different pipelines run this
# against the same service at the same time; nothing serializes them.
set -euo pipefail

export CHAMBER_WRITER_ID="${CI_PIPELINE_NAME:-manual}/${CI_JOB_ID:-local}"
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"

# Hard step timeout. A write that has not finished after 20 s is SIGKILLed: no cleanup runs, and
# whatever it had already sent to Secrets Manager stays there.
exec timeout --signal=KILL 20 python3 -m chambersm write "$@"
